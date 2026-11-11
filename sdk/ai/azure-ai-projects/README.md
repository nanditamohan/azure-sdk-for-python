# Azure AI Projects client library for Python

The AI Projects client library is part of the Microsoft Foundry SDK, and provides easy access to
resources in your [Microsoft Foundry](https://ai.azure.com/) Project. Use it to:

* **Create and run Agents** using methods on the `.agents` client property. This includes **Hosted Agents**, which let you run your own containerized agent runtime while using Microsoft Foundry for managed hosting and scaling.
* **Build and run Voice Agents (preview)** for real-time, speech-to-speech conversational AI, reachable over a WebSocket (`.beta.voice_agents.realtime`) or telephony (`.beta.voice_agents.telephony`), with persisted conversation transcripts and audio through `.beta.voice_agents.conversations`.
* **Enhance Agents with specialized tools and toolbox tools** such as:
  * Agent-to-Agent (A2A)
  * Azure AI Search
  * Azure Functions
  * Bing Custom Search (Preview)
  * Bing Grounding
  * Browser Automation (Preview)
  * Code Interpreter
  * Computer
  * Computer Use (Preview)
  * Fabric IQ (Preview)
  * File Search
  * Function Tool
  * Image Generation
  * Memory Search (Preview)
  * Microsoft Fabric (Preview)
  * Microsoft SharePoint (Preview)
  * Model Context Protocol (MCP)
  * OpenAPI
  * Reminder Tool (Preview)
  * Toolbox Search
  * Web Search
  * Work IQ (Preview)
* **Get an OpenAI client** using `.get_openai_client()` method to run Responses, Conversations, Evaluations and Fine-Tuning operations with your Agent.
* **Create and version toolboxes** that bundle collections of tools and skills for your agents, using `.toolboxes` operations.
* **Fine-tune** AI Models on your data.
* **Enumerate AI Models** deployed to your Foundry Project using `.deployments` operations.
* **Enumerate connected Azure resources** in your Foundry project using `.connections` operations.
* **Upload documents and create Datasets** to reference them using `.datasets` operations.
* **Create and enumerate Search Indexes** using `.indexes` operations.
* **Explore additional evaluation tools (some in preview)** to assess the performance of your generative AI application, using `.evaluation_rules`,
`.beta.evaluation_taxonomies`, `.beta.evaluators`, `.beta.insights`, and `.beta.schedules` operations.
* **Manage memory stores (preview)** for Agent conversations, using `.beta.memory_stores` operations.
* **Register and manage local AI model weights (preview)** using `.beta.models` operations.
* **Run Red Team scans (preview)** to identify risks associated with your generative AI application, using `.beta.red_teams` operations.
* **Use Reinforcement Learning Environments (RLE) (preview)** through `.rle` with `allow_preview=True` to create, manage, and run hosted environments, including text and binary OpenEnv WebSocket interactions.
* **Create and manage routines (preview)** that execute triggered actions based on configured conditions, using `.beta.routines` operations.
* **Manage skills (preview)** as reusable capabilities that can be included in toolboxes and agents, using `.beta.skills` operations.

**Important:** This stable package includes Preview features. Preview features available through stable methods require setting `allow_preview=True` when constructing the client, while Preview features exposed via the .beta sub-clients do not. Preview features may change or be removed in future versions and are not recommended for production use.

The client library uses version `v1` of the Microsoft Foundry [data plane REST APIs](https://aka.ms/azsdk/azure-ai-projects-v2/api-reference-v1).

[Microsoft Foundry](https://ai.azure.com/)
| [Documentation](https://aka.ms/azsdk/azure-ai-projects-v2/product-doc)
| [Samples][samples]
| [API reference](https://aka.ms/azsdk/azure-ai-projects-v2/python/api-reference)
| [Package (PyPI)](https://aka.ms/azsdk/azure-ai-projects-v2/python/package)
| [SDK source code](https://aka.ms/azsdk/azure-ai-projects-v2/python/code)
| [Release history](https://aka.ms/azsdk/azure-ai-projects-v2/python/release-history)

## Reporting issues

To report an issue with the client library, or request additional features, please open a [GitHub issue here](https://github.com/Azure/azure-sdk-for-python/issues). Mention the package name "azure-ai-projects" in the title or content.

## Getting started

### Prerequisite

* Python 3.9 or later.
* An [Azure subscription][azure_sub].
* A [project in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/how-to/create-projects).
* A Foundry project endpoint URL of the form `https://your-ai-services-account-name.services.ai.azure.com/api/projects/your-project-name`. It can be found in your Microsoft Foundry Project home page. Below we will assume the environment variable `FOUNDRY_PROJECT_ENDPOINT` was defined to hold this value.
* Client authentication is done using Entra ID. To authenticate, your application needs an object that implements the [TokenCredential](https://learn.microsoft.com/python/api/azure-core/azure.core.credentials.tokencredential) interface. Code samples here use [DefaultAzureCredential](https://learn.microsoft.com/python/api/azure-identity/azure.identity.defaultazurecredential). To get that working, you will need:
  * An appropriate role assignment. See [Role-based access control in Microsoft Foundry portal](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry). Role assignment can be done via the "Access Control (IAM)" tab of your Azure AI Project resource in the Azure portal.
  * [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed.
  * You are logged into your Azure account by running `az login`.

### Install the package

```bash
pip install azure-ai-projects
```

Verify that you have version 2.3.0 or above installed by running:

```bash
pip show azure-ai-projects
```

## Key concepts

### Create and authenticate the client with Entra ID

Entra ID is the only authentication method currently supported by the client.

To construct a synchronous client using a context manager:

```python
import os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

with (
    DefaultAzureCredential() as credential,
    AIProjectClient(
        endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        credential=credential
    ) as project_client,
):
```

To construct an asynchronous client, install the additional package [aiohttp](https://pypi.org/project/aiohttp/):

```bash
pip install aiohttp
```

and run:

```python
import os
import asyncio
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential

async with (
    DefaultAzureCredential() as credential,
    AIProjectClient(
        endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        credential=credential
    ) as project_client,
):
```

### Performing Responses operations using OpenAI client

Use the `.get_openai_client()` method to obtain an authenticated [OpenAI](https://github.com/openai/openai-python) client and run Responses, Conversations, Evaluations, Files, and Fine-Tuning operations. See the **responses**, **agents**, **evaluations**, **files**, and **finetuning** folders in the [samples][samples] for complete working examples.

The code below assumes the environment variable `FOUNDRY_MODEL_NAME` is defined. It's the deployment name of an AI model in your Foundry Project. See first column titled "Name" in the "Deployed models" table in your Foundry Project.

<!-- SNIPPET:sample_responses_basic.responses -->

```python
with project_client.get_openai_client() as openai_client:
    response = openai_client.responses.create(
        model=os.environ["FOUNDRY_MODEL_NAME"],
        input="What is the size of France in square miles?",
    )
    print(f"Response output: {response.output_text}")

    response = openai_client.responses.create(
        model=os.environ["FOUNDRY_MODEL_NAME"],
        input="And what is the capital city?",
        previous_response_id=response.id,
    )
    print(f"Response output: {response.output_text}")
```

<!-- END SNIPPET -->

See the **responses** folder in the [samples][samples] for additional samples including streaming responses.

### Agents

See Foundry documentation:

* **[Microsoft Foundry Agents overview](https://learn.microsoft.com/azure/foundry/agents/overview)** — concepts, setup, and quick-starts.
* **[Runtime components](https://learn.microsoft.com/azure/foundry/agents/concepts/runtime-components?tabs=python)** — deep-dive into agent architecture.
* **[Tool catalog](https://learn.microsoft.com/azure/foundry/agents/concepts/tool-catalog)** — all available tools and agent capabilities.
* **[SDK samples folder][samples]** — fully runnable Python code for synchronous and asynchronous clients covering all operations below.

## Examples

The table below lists the operation groups supported by the client library, with links to Foundry documentation and relevant [samples][samples] sub-folder. Additional documentation and samples may have been added after this package was released to cover the empty cells below.

| Topic | Foundry documentation | Samples folder |
|---|---|---|
| Agents (create, run, stream) | [Agents overview](https://learn.microsoft.com/azure/foundry/agents/overview) | `samples/agents/` |
| Agent Insights (preview) | | [On-demand analysis and scheduled monitors](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects/samples/agent_insights) |
| Hosted agents | [Hosted agents concepts](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents), [Deploy your first hosted agent](https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-hosted-agent) | `samples/hosted_agents/` |
| Agents tools | [Tool catalog](https://learn.microsoft.com/azure/foundry/agents/concepts/tool-catalog) | `samples/agents/tools/` |
| Agents optimization | [Prompt optimizer](https://learn.microsoft.com/azure/foundry/observability/how-to/prompt-optimizer), [Agent optimizer overview](https://learn.microsoft.com/azure/foundry/agents/concepts/agent-optimizer-overview) | `samples/agents/optimization/` |
| Connections | [Add a new connection to your project](https://learn.microsoft.com/azure/foundry/how-to/connections-add?tabs=foundry-portal)| `samples/connections/` |
| Client-side tracing | [Add client-side tracing to Foundry agents](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-client-side?tabs=python) | `samples/telemetry/` |
| Datasets | | `samples/datasets/` |
| Deployments | [Deployment types](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/deployment-types) | `samples/deployments/` |
| Evaluation | [Evaluate agents](https://learn.microsoft.com/azure/foundry/observability/how-to/evaluate-agent) | `samples/evaluations/` |
| Files (upload, retrieve, list, delete) | [OpenAI Files API](https://platform.openai.com/docs/api-reference/files) | `samples/files/` |
| Fine-tuning | [When to use fine-tuning](https://learn.microsoft.com/azure/foundry/openai/concepts/fine-tuning-considerations), [Fine-Tuning in AI Foundry (GitHub repo)](https://github.com/microsoft-foundry/fine-tuning) | `samples/finetuning/` |
| Indexes | [Azure AI Search](https://learn.microsoft.com/azure/search/search-what-is-azure-search) | `samples/indexes/` |
| Memory stores (preview) | [What is memory?](https://learn.microsoft.com/azure/foundry/agents/concepts/what-is-memory?tabs=conversational-agent) | `samples/memories/` |
| Models (preview) | | `samples/models/` |
| Red teams (preview) | | `samples/red_team/` |
| Reinforcement Learning Environments (RLE) (preview) | | `samples/rle/` |
| Responses | [Responses API](https://platform.openai.com/docs/api-reference/responses) | `samples/responses/` |
| Routines (preview) | [Routines overview](https://learn.microsoft.com/azure/foundry/agents/concepts/routines) | `samples/hosted_agents/` |
| Sessions | [Manage hosted sessions](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions?pivots=python) | `samples/hosted_agents/` |
| Skills (preview) | | `samples/skills/` |
| Toolboxes | [Curate intent-based toolbox in Foundry](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox?pivots=python) | `samples/hosted_agents/`, `samples/toolboxes/` |
| Voice agents (preview) | | `samples/agents/voice/` |

### Author a Foundry RLE environment

Install the optional RLE environment-authoring dependencies:

```bash
pip install "azure-ai-projects[rle]"
```

Subclass `RLEnvironment` and implement `reset()` and `grade()`. MCP tools
are optional. Register them after `super().__init__()` with the inherited
`tool()` decorator:

```python
from typing import Any

from openenv.core.env_server import Observation, State

from azure.ai.projects.rle.environments import GradeAction, RLEnvironment, create_app


class OrderEnvironment(RLEnvironment):
    def __init__(self) -> None:
        super().__init__()

        @self.tool()
        def lookup_order(order_id: str) -> dict[str, str]:
            return {"order_id": order_id, "status": "shipped"}

    def reset(
        self,
        seed: int | None = None,
        episode_id: str | None = None,
        **kwargs: Any,
    ) -> Observation:
        self._set_state(State(episode_id=episode_id))
        return Observation(metadata={"question": "What is the order status?"})

    def grade(
        self,
        action: GradeAction,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> Observation:
        is_correct = action.response == "shipped"
        return Observation(reward=float(is_correct), done=True)


app = create_app(
    OrderEnvironment,
    GradeAction,
    Observation,
    state_cls=State,
    env_name="order_environment",
)
```

Pass `GradeAction` as the action class when creating the OpenEnv server so
WebSocket step payloads are deserialized before `grade()` is called. If tools
are already registered on a `FastMCP` server, pass it to
`RLEnvironment(mcp_server=server)` from the subclass constructor.

`GradeAction` also carries an optional `context`: everything RLE knows about the
finished rollout. It holds an ATIF v1.8 `trace` of the captured model calls, the
declared `output_format` and structured final `output`, why the run stopped
(`termination_reason`), the original `task`, native environment `rewards` and
`env_report`, the rollout `id`, and the contract `version`. Read it through
`action.grade_context`, a read-only mapping that also supports attribute access,
so `context.output` and `context["output"]` are the same value:

`output_format="custom"` accepts an object or array. Built-in environments use
`{"text": "<final answer>"}`; other structures need a task-specific reader.
`output_format="responses"` preserves the native Responses output array. Read
message content of type `output_text`, not reasoning or tool-call items.
There are no canonical top-level `response_text` or `outputs` fields.
For JSONL tasksets, `task` is the resolved row with its grading metadata;
`rewards` and `env_report` are empty, so the grader must derive correctness.

This example expects the built-in custom text convention:

```python
    def grade(
        self,
        action: GradeAction,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> Observation:
        context = action.grade_context
        looked_up = any(
            call["function_name"] == "lookup_order"
            for step in context.trace.get("steps", [])
            for call in step.get("tool_calls", [])
        )
        if context.output_format != "custom" or not isinstance(context.output, dict):
            raise ValueError("This grader requires custom text output")
        answer = context.output.get("text")
        if not isinstance(answer, str):
            raise ValueError("Custom output must contain a string text field")
        is_correct = answer == "shipped" and looked_up
        return Observation(reward=float(is_correct), done=True)
```

Every field is always present. Missing or null fields read as empty (`""`,
`{}` or `0` for `version`), except `output_format`, which defaults to `"custom"`.
Missing or null `output` is `[]` for `"responses"` and `{}` otherwise.
Within a `version` RLE only adds fields, so ignore keys you do not know,
and treat a `termination_reason` you do not recognize as `"unknown"`.

RLE sends `context` only when the environment's `GET /schema` action schema
declares it, which `GradeAction` does. Environments built on an older SDK or on
a custom action class keep receiving only `response`, as before. The same holds
for a service that predates the context: `grade_context` then returns a
`version` `0` context with `output_format="custom"` and
`output={"text": response}`, so one `grade()` implementation covers both.
Older contexts containing `response_text` but neither output field are also
adapted to custom text output. Original unknown keys remain readable, but the
SDK does not synthesize `response_text` or `outputs` aliases for new contexts.

Use the SDK's `create_app` factory (same arguments and `FastAPI` return value
as OpenEnv 0.6) to install request-scoped session routing automatically. Forward
the supplied MCP endpoint, including its `rle_session_id` query parameter,
unchanged and send ordinary `tools/list` and `tools/call` JSON-RPC requests.
The query value is decoded once, consumed, and overwrites `params.session_id`;
it is never added to tool arguments or schemas. Requests without the query keep
OpenEnv's existing body-only routing. Other query parameters, lifecycle methods,
non-MCP routes, WebSockets, and response streaming retain their behavior.

Exactly one nonempty session query value is required when present. Invalid
query values and request shapes return OpenEnv-style JSON-RPC errors.
Query-adapted request bodies are bounded to 100 MiB (OpenEnv 0.6 has no HTTP
body-size limit); oversized bodies return HTTP 413, and compressed bodies are
rejected. OpenEnv's existing unsupported initialization, notifications, and batch
behavior is unchanged.

Treat session-bearing URLs as private runtime metadata: they can appear in
agent-side access logs. Keep bearer tokens separate from the URL. Session
selection is not authorization; the middleware does not verify session ownership.


## Client-side tracing

See [Add client-side tracing to Foundry agents (preview)](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-client-side?tabs=python).

**Important:** GenAI tracing instrumentation is an experimental preview feature. Spans, attributes, and events may be modified in future versions. 

Samples can be found in the sub-folders `agents/telemetry` and `telemetry` in the [Samples][samples] folder.

## Troubleshooting

### Exceptions

Client methods that make service calls raise an [HttpResponseError](https://learn.microsoft.com/python/api/azure-core/azure.core.exceptions.httpresponseerror) exception for a non-success HTTP status code response from the service. The exception's `status_code` will hold the HTTP response status code (with `reason` showing the friendly name). The exception's `error.message` contains a detailed message that may be helpful in diagnosing the issue:

```python
from azure.core.exceptions import HttpResponseError

...

try:
    result = project_client.connections.list()
except HttpResponseError as e:
    print(f"Status code: {e.status_code} ({e.reason})")
    print(e.message)
```

For example, when you provide wrong credentials:

```text
Status code: 401 (Unauthorized)
Operation returned an invalid status 'Unauthorized'
```

### Logging

The client uses the standard [Python logging library](https://docs.python.org/3/library/logging.html). The logs include HTTP request and response headers and body, which are often useful when troubleshooting or reporting an issue to Microsoft.

#### Default console logging

To turn on client console logging define the environment variable `AZURE_AI_PROJECTS_CONSOLE_LOGGING=true` before running your Python script. Authentication bearer tokens are automatically redacted from the log. Your log may contain other sensitive information, so be sure to remove it before sharing the log with others.

#### Customizing your log

Instead of using the above-mentioned environment variable, you can configure logging yourself and control the log level, format, and destination. You can optionally attach the same handler to the Azure SDK logger and, for `.get_openai_client()` scenarios, optionally attach it to the dedicated OpenAI transport logger as well:

```python
import sys
import logging

# Direct logging output to stdout:
handler = logging.StreamHandler(stream=sys.stdout)
# Or direct logging output to a file:
# handler = logging.FileHandler(filename="sample.log")

# Optional: logger for azure-ai-projects and azure-core.
logger = logging.getLogger("azure")
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)

# Optional: additional logger for an openai client generated from `.get_openai_client()`.
openai_logger = logging.getLogger("azure.ai.projects.openai_transport")
openai_logger.setLevel(logging.DEBUG)
openai_logger.propagate = False
openai_logger.addHandler(handler)


# Optional: change the default logging format. Here we add a timestamp.
#formatter = logging.Formatter("%(asctime)s:%(levelname)s:%(name)s:%(message)s")
#handler.setFormatter(formatter)
```

By default logs redact the values of URL query strings, the values of some HTTP request and response headers (including `Authorization` which holds the key or token), and the request and response payloads. To create logs without redaction, add `logging_enable=True` to the client constructor:

```python
project_client = AIProjectClient(
    credential=DefaultAzureCredential(),
    endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    logging_enable=True
)
```

Note that the log level must be set to `logging.DEBUG` (see above code). Logs will be redacted with any other log level.

See the logging samples in the `samples/logs/` folder for complete end-to-end examples, including console logging, file logging, and OpenAI transport logging.

Be sure to protect non-redacted logs to avoid compromising security.

For more information, see [Configure logging in the Azure libraries for Python](https://aka.ms/azsdk/python/logging)

### Reporting issues

To report an issue with the client library, or request additional features, please open a [GitHub issue here](https://github.com/Azure/azure-sdk-for-python/issues). Mention the package name "azure-ai-projects" in the title or content.

## Shared RLE rollout sessions (private preview)

### Model sampling defaults

Pass `SamplingOptions` to HTTP or shared-session `execute_rollout` calls:

```python
from azure.ai.projects.rle import SamplingOptions

sampling = SamplingOptions(
    max_completion_tokens=32768,
    temperature=0.7,
    top_p=1.0,
    top_k=-1,
    seed=42,
)
# Add sampling=sampling to your existing execute_rollout call.
```

All fields are optional defaults for each model call through Capture Proxy.
Explicit model-request values override them; operator ceilings and remaining
context still apply. A requested maximum is not a guaranteed response length.
Training capture requires neutral `top_p=1` and `top_k=-1` or `0`. Evaluation
provider support varies, and a seed does not guarantee reproducibility.
The SDK does not read local `rle.toml`; that configuration is CLI-only.

### Legacy sessions (default)

Construct the client with `allow_preview=True`. For training, open one session for the
published environment version and share it across rollout calls:

```python
import asyncio
from azure.ai.projects.rle import HarnessConfiguration, LoomPolicy

policy = LoomPolicy(
    model_name="model",
    project_endpoint="https://account.services.ai.azure.com/api/projects/project",
    session_id="training-session",
    checkpoint_id="checkpoint",
)

# project_client is azure.ai.projects.aio.AIProjectClient.
async with project_client.rle.get_rollout_client(
    environment_name="environment",
    environment_version="1",
    job_id="ftjob-training-run",  # Optional; omit outside a job context.
) as session:
    results = await asyncio.gather(
        *(
            session.execute_rollout(
                task=task,
                policy=policy,
                phase="train",
                harness=HarnessConfiguration(type="gym"),
                timeout=1860,
                max_response_bytes=32 * 1024 * 1024,
            )
            for task in tasks
        )
    )
```

Shared sessions are asynchronous only. Entry is lazy: the first execution obtains
credentials and opens the socket. Use `await session.close()` without a context manager,
and close the session before the project client. Existing sync and async
`client.rle.execute_rollout(...)` retain their HTTP default and do not create a session.
Shared sessions default to V4 and require a V4-capable backend; there is no automatic
fallback. Select `protocol_version="v3"` for V3 or explicitly pass
`protocol_version=None` for legacy v2/v1.

`job_id` optionally associates rollouts with a job. Omit it when running outside
a job context; no job identifier is generated. Each execution still has its own
`rollout_id`, generated by the SDK when omitted.

Pass `phase="train"` for training rollouts or `phase="eval"` for validation and
final evaluation. This optional top-level request field is supported by both
HTTP operations and the shared async rollout client. Omission or `None` leaves
the field absent so the service applies its default (`train` for new records);
other values are rejected before submission. The RLE service must support this
field. Phase does not change `policy.sequence_id` or checkpoint selection:
training and evaluation using the same weights can share a policy sequence.

Both HTTP operations and the shared rollout client use `HarnessConfiguration`
for an optional per-rollout override. Omitting it uses the environment
version's stored harness. For BYOH Responses, pass the agent base URL as `endpoint`;
the service adds the Responses protocol path. Package execution requires service support.

Outbound execute messages are limited to 4 MiB including their length prefix,
header and payload, with a separate 4 KiB header limit. Oversized requests fail
locally before submission and do not interrupt other pending rollouts.

The SDK handles rollout concurrency internally and owns **one active submission socket**.
Each client admits up to 32 concurrent rollouts; no public concurrency override is exposed.
Sends are serialized and one receive dispatcher
correlates out-of-order results by rollout ID. `progress_callback`, if supplied, receives
a mapping containing `rollout_id`; it must be a lightweight synchronous callable.
Callback exceptions and malformed progress are surfaced to the associated caller.

The following connection renewal, deployment-drain and HTTP status-recovery details
describe explicit legacy sessions (`protocol_version=None`). Default V4 sessions use
the durable attachment and result retrieval described in the queued-session section below.

For long jobs, the SDK stops new submissions at the age/token renewal boundary, waits
for current pending calls to settle, then closes the old socket before opening its replacement.
Deployment draining is different: existing work stays on the draining socket while new
submissions open a replacement, without waiting for existing work to finish.
Renewal is scheduled after at most
1740 seconds or 60 seconds before credential expiry, whichever comes first.
Every new connection refreshes credentials.
Credentials must report their actual expiry in `AccessToken.expires_on`. In particular,
adapters using a synthetic short expiry to control HTTP caching should supply a session-only
credential with the actual provider expiry, rather than forcing rotation on every call.
Tokens with 60 seconds or less remaining are rejected before submission.
The handshake supplies both `Authorization` and `aml-user-token`; both are redacted by
the SDK WebSocket logger.

For deployments with server-side delegated token renewal, pass
`server_managed_token_renewal=True` and `protocol_version=None` to
`get_rollout_client`. This disables
age/token-based replacement of a healthy socket; tokens are still acquired and
validated for every initial or replacement handshake. No user token is sent in
individual execute frames. RLE must renew the connection caller's token before
downstream rollout setup, and Capture Proxy must renew tokens during model calls.
The service must also support long-lived connections and the requested execution
timeout. The current 30-minute service limits are not lifted by this option.
Use this opt-in only after those service changes are deployed. The default remains
the existing renewal policy. Deployment-drain handoff and disconnected-rollout
recovery are supported with either setting.

Connections reuse the existing OpenEnv `_connect_openenv_websocket` helper:
up to three attempts for transient connectivity failures or HTTP 408, 429, 500,
502, 503 and 504, with per-attempt timeouts up to 25 seconds, a 90-second connection
budget and full-jitter delays up to two and four seconds. The remaining rollout
deadline can shorten these bounds. Authentication and certificate failures are not retried.
These are opening-handshake retries only. Execution errors are surfaced rather
than replayed, except for the explicit `ServerDraining` admission rejection below. Generic `retryable=true`
can describe a dependency failure after work started, not just an admission rejection.

Legacy terminal WebSocket errors preserve the service's explicit boolean retry hint as
`RLERolloutError.retryable`; missing or nonboolean values default to `False`.
The SDK does not automatically retry those failures. Applications can use this
hint for a bounded, fresh-ID reattempt after a confirmed terminal failure, but
must never replay `RLERolloutUnknownOutcomeError`. Replacing a shared client
must wait until all its pending calls have settled.
Queued V3/V4 terminal failures remain nonretryable. A nested
`cause.retryable=true` describes a dependency, not authorization to replay a
terminal execution; it never sets `RLERolloutError.retryable`.

If a socket closes with unfinished rollouts, only those rollouts switch to HTTP
status/result lookup using their **original** IDs and API version `2025-05-01`.
Running rollouts remain pending; completed results resolve the original awaiting
calls, and failed rollouts raise terminal errors. One client-wide polling worker
owns all recovering IDs. It waits 30 seconds before its first round, then waits
30 seconds between subsequent rounds. Each round checks pending IDs individually
in parallel; there is no exponential backoff or separate timer per rollout.
If a replacement socket also closes, its unfinished IDs join the same list and
next scheduled round without restarting the timer. Once the list empties,
the worker stops; a later recovery batch starts with the same 30-second wait.
Missing records/results and transient lookup failures wait for the next shared
round, bounded by each rollout's original deadline. HTTP pipeline retries are
disabled for these lookups so there is only one polling schedule.
Healthy sockets, quiet rollouts, normal credential rotation, and still-connected
draining sockets are not polled. Opening a replacement alone does not trigger
recovery. Recovering rollouts retain their concurrency permits while new work can
use a replacement socket within the same admission budget.
Execution is never replayed to recover a missing result. Result persistence is
best-effort: missing or expired artifacts can leave an unknown outcome even after
the backend finishes. An interrupted send, unsuccessful recovery deadline,
submitted cancellation or session closure raises `RLERolloutUnknownOutcomeError`
with the original ID. Authentication, malformed metadata and mismatched results
surface explicit errors rather than being treated as successful recovery.
An unknown outcome affects only that rollout: its local permit is released,
queued and future calls can proceed, and other rollouts can still complete or
recover. The backend may still be executing the unknown rollout; the SDK does not
cancel or replay it. The concurrency limit bounds calls still tracked by the client,
not backend executions whose outcomes have become unknown.
WebSocket execution uses API version `2025-11-15-preview`.
The default `timeout=1860` begins **after SDK concurrency admission**: queue waiting
does not consume a rollout's timeout, so a large job is not given a job-wide 1860-second
deadline. Queue waiting remains cancellable and awakens promptly on closure or a
session-level protocol failure, not an unrelated unknown rollout outcome.
After admission, timeout includes renewal waiting, credentials,
opening the connection, serialized send waiting, execution and any disconnect
recovery. Reconnecting never resets this deadline.
Timeout/cancellation before attempting to send does not submit work or itself block future admission.

On a `server-draining` notification, the SDK stops submitting to that connection.
Its receiver continues dispatching progress and terminal results for existing rollouts.
The next submission opens a replacement with fresh credentials; admission remains global
across all connections, and an old connection is closed after its pending calls settle.
Repeated notifications do not open additional sockets. Multiple successive deployments
can leave several old connections delivering their original results.

A rollout explicitly rejected with `ServerDraining` was not started by the service.
Only this admission rejection is resubmitted, with the same rollout ID and request,
at most three submissions and a one-second delay between attempts. The original
post-admission deadline and concurrency permit cover all attempts. The service must
preserve this pre-execution rejection contract; a generic retryable error is not enough.
No signal migrates an already-running rollout to another pod. If the old socket
disconnects, unfinished rollouts use HTTP recovery while new work can use the
replacement socket. If the backend cannot finish or retain a retrievable result,
recovery ends in an explicit unknown outcome at the original deadline.

Cancellation, timeout, and session close do not cancel server execution or siblings.
If a submitted outcome cannot be accounted for, the session stops admitting new work;
do not create a replacement session blindly and assume that server capacity was released.
Queued callers are awakened by closure, receiver tasks are cancelled, and socket cleanup
is bounded to five seconds. Submitted unresolved work raises an explicit unknown-outcome
error. An already-dispatched completion wins a cancellation or disconnect race.
No per-rollout cancel commands are issued. `max_response_bytes` is a positive per-call safety
budget; a larger configured budget is accepted, but the WebSocket's 8 MiB payload bound still
applies. Frame headers are limited to 4 KiB and reject duplicate JSON keys.

Deployment-drain handoff is in **2.7.0b5 (Unreleased)** and requires publishing that
development wheel before consumers pin it.

### Durable queued sessions (V4 by default)

Use the same asynchronous public factory and typed request/result models:

```python
async with project_client.rle.get_rollout_client(
    environment_name="environment",
    environment_version="1",
    job_id=training_job_id,  # Optional; preserve the actual training job ID unchanged.
    finetune_credential=finetune_credential,  # Optional, caller-owned app identity.
) as session:
    result = await session.execute_rollout(
        task=task,
        policy=policy,
        harness=HarnessConfiguration(type="gym"),
        timeout=1860,
    )
```

Omitting `protocol_version` selects V4. Explicit `"v3"` or `"v4"` offers **only** its corresponding
`rle.execute-rollout.v3` or `rle.execute-rollout.v4` subprotocol and verifies
the selected protocol. Disabled delivery, failed negotiation or disabled queued
acceptance is an explicit error, never a fallback to legacy execution. Synchronous
and asynchronous `rle.execute_rollout()` retain their HTTP defaults; shared
WebSocket sessions remain asynchronous only. To retain legacy v2/v1 negotiation
and lifecycle, explicitly pass `protocol_version=None`.

V3/V4 `accepted` means durable admission, **not completion**. The SDK keeps the call
pending through queued/running/finalizing state and worker retries, dispatches
durable progress, and returns a full `RolloutResult` only on committed completion.
Capture graphs, token arrays, rewards, and structured results retain the same
typed mapping as legacy results. Progress callbacks receive `rollout_id`,
`sequence`, and `attempt_id` alongside the progress fields; keep callbacks short
and synchronous. Error messages are sanitized; inspect `RLERolloutError.code`
for known service codes such as `QueuedAcceptanceDisabled`, `HistoryUnavailable`,
or `queued_execution_failed`. Unknown service codes become `QueuedRolloutFailed`.
History/dependency errors are not fabricated successes or automatic re-execution.

New service versions also provide a structured `RLERolloutError.cause` for grader
failures, with `code`, `dependency`, `operation`, `retryable`, and optional
`error_type`. `service_message` preserves the bounded caller-facing reason;
`attempt_id`, `sequence`, and `rollout_id` identify the terminal execution.
These fields survive attachment recovery, including terminals synthesized from
durable state. Older failures without a cause keep the generic message. Cause
identifiers are validated, unknown cause codes remain available, and extra fields
such as traceback are discarded. Treat the reason as customer content: do not
log it to shared telemetry. Nested cause retryability describes the dependency;
it does not authorize replaying a terminal rollout.

V4 uses the same durable execution as V3, but every `completed` event contains
only the durable event identity and `result_available: true`, never a graph or
result reference. The SDK automatically calls
`GET /rl_environments/rollouts/{rolloutId}/result` at the original project endpoint
and returns the full `RolloutResult`. This applies to small and large results alike.
Retrieval runs outside the socket receiver, so it does not block other rollouts.
The SDK does not save local files; callers can keep using `result.as_dict()` and
their existing persistence or training code. The original timeout and
`max_response_bytes` still bound the complete download. V4 defaults to **32 MiB
(33,554,432 bytes)**; increase the per-call budget with
`execute_rollout(..., max_response_bytes=64 * 1024 * 1024)` for 64 MiB, or supply a
smaller positive limit. Omitting the parameter or passing `None` selects the
protocol default. V3 and legacy defaults remain 8 MiB, and WebSocket payloads
remain capped at 8 MiB regardless of the V4 download budget.
Exceeding the download budget raises `RLERolloutError` with code
`RolloutResultTooLarge`; the message includes the configured limit, bytes observed,
and an example `max_response_bytes` override. Only result retrieval fails, not the
completed rollout. A retrieval failure is reported without re-executing the rollout,
even if the socket has since closed. The full response is buffered and parsed in
memory, so choose a budget appropriate for the calling process.
The current service graph is available as `result.rollout_graph`; legacy
`result.rollout` remains supported, and `result.sequences` reads either shape.

One serialized command driver and an independent bounded receiver multiplex up to
32 admitted logical calls. The original post-admission deadline and admission
permit cover connection, execution, attachment recovery and complete result fetch.
Queue waiting remains cancellable and wakes on close. On disconnect or credential
rotation, submitted IDs are **attached**, never executed again, including after an
ambiguous initial send. Each logical rollout permits at most three reattachments;
consecutive connection failures are also bounded. Recovery never resets the timeout.
The attach cursor is the last durable sequence actually received, including zero,
or null before any event. State snapshots never advance it. Gaps, changed worker
attempt IDs and replay of the committed terminal sequence are valid.

V3/V4 rollout IDs must be lowercase GUIDs without separators; SDK-generated IDs
already satisfy this. Optional job IDs are opaque strings with a case-sensitive
`ftjob-` prefix, only ASCII letters, digits, `.`, `_`, `~`, and `-`, and at most
991 characters total. There is no fixed suffix length or hexadecimal requirement;
both 24- and 32-hex managed forms are accepted. IDs are never truncated, normalized,
or replaced. The project endpoint and exact environment name/version fix connection
scope; every command retains the original job/rollout correlation.

The project credential supplies `Authorization` and `aml-user-token`.
An optional separate `finetune_credential` supplies `x-ms-finetune-identity` on
handshake and result retrieval, preserving the consumed 2.7.0b7 identity contract.
Both use the project's configured scopes and require real expiry more than
60 seconds away. Connection lifetime is bounded by the earlier expiry and
1740 seconds. Both credentials remain caller-owned. Separate fine-tuning
credentials and V3/V4 cannot use `server_managed_token_renewal=True`.
V3/V4 wire debug logging is disabled to avoid exposing credentials or training data.

Large-result references must be canonical, correlated relative RLE result routes,
not absolute URLs, Blob URLs or SAS URLs. The SDK retrieves the complete result
through the **original configured project endpoint**, using fresh authenticated
headers, no redirects and a streamed response-size bound. A summary is never
substituted for the result. Unavailable, malformed, mismatched and oversized
results produce explicit errors.

Cancellation, detach, timeout and close affect local delivery only; they do not
cancel remote work or prove capacity was released. A submitted unresolved call
raises `RLERolloutUnknownOutcomeError` with its original ID. Never automatically
choose a new ID and rerun it. Durable admission is not a guarantee of worker crash
recovery: the SDK does not assume a server dispatcher or Running-work reclaim.
Service gates, worker capacity, retention and authorization must be configured
separately. Building/pinning this development wheel does not deploy the service or
rebuild/select a training image.

SDK API conventions follow the [Azure SDK Python Design Guidelines](https://azure.github.io/azure-sdk/python_design.html).

## Next steps

Have a look at the [Samples][samples] folder, containing fully runnable Python code for synchronous and asynchronous clients.

## Contributing

This project welcomes contributions and suggestions. Most contributions require you to agree to a Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us the rights to use your contribution. For details, visit https://cla.microsoft.com.

When you submit a pull request, a CLA-bot will automatically determine whether you need to provide a CLA and decorate the PR appropriately (e.g., label, comment). Simply follow the instructions provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct][code_of_conduct]. For more information, see the Code of Conduct FAQ or contact opencode@microsoft.com with any additional questions or comments.

<!-- LINKS -->
[samples]: https://aka.ms/azsdk/azure-ai-projects-v2/python/samples/
[code_of_conduct]: https://opensource.microsoft.com/codeofconduct/
[azure_sub]: https://azure.microsoft.com/free/
