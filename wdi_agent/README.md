# World Bank WDI agent (LangGraph + Gemini + BigQuery)

Answers questions about World Bank development data in plain language, such as "What is the GDP
of India?" or "How fast did Kenya's population grow from 2010 to 2020?". Gemini on Vertex AI
picks tools, the tools read Google's public copy of the
[World Development Indicators](https://datacatalog.worldbank.org/search/dataset/0037712) in
BigQuery (`bigquery-public-data.world_bank_wdi`), and Gemini writes a short answer with the
unit, year and source.

**No internet access needed.** The agent only calls Google APIs (Vertex AI and BigQuery), so it
runs in Agent Engine without a proxy, NAT or private network.

```text
question ─► Gemini (tools bound) ─► tool calls? ──yes──► BigQuery WDI / calculator ─┐
                 ▲                        │no                                        │
                 └──────── results ───────┼◄────────────────────────────────────────┘
                                          ▼
                                       answer
```

## Tools

Gemini never writes SQL. Each tool runs a fixed, parameterised query, or local maths:

| Tool | Does |
| --- | --- |
| `wdi_find_country` | Country or aggregate name/code → ISO3 code, region, income group |
| `wdi_search_indicators` | Keywords → indicator codes, names and units |
| `wdi_get_data` | One indicator for up to 15 countries, for a year range or the latest year |
| `wdi_calculate` | Percent change, CAGR, difference, ratio, share, readable numbers (no network) |

Inputs are validated before any query runs, every query has a byte limit
(`WDI_MAX_BYTES_BILLED`, default 5 GiB) and returns at most 500 rows.

## Deploy with the console

1. **Ship** → Local folder `wdi_agent`. The folder's `agentspec.yaml` sets the framework
   (LangGraph), entrypoint, dependencies, resources and settings.
2. Review, approve, deploy. Then **Chat** from the Agent garden.

`--poc` mode (default Agent Engine identity, standard networking) is enough.

### One permission

Queries run, and are billed, in your project, so the agent's identity needs the BigQuery
**Job User** role. With `--poc`, that's the Agent Engine service agent. Get your project
number, then grant the role, replacing `PROJECT_NUMBER`:

```bash
gcloud projects describe itsai-mai-gcp-lab-proj --format="value(projectNumber)"
```

```bash
gcloud projects add-iam-policy-binding itsai-mai-gcp-lab-proj --member="serviceAccount:service-PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com" --role="roles/bigquery.jobUser"
```

Reading the public dataset needs no extra grant. If the project is inside a VPC Service
Controls perimeter, the perimeter must allow reading `bigquery-public-data`; without that the
tools return a "BigQuery refused the query" error that says so.

## Things to know

* **Data freshness.** The public BigQuery copy can lag the World Bank's latest release, so the
  newest year may be missing. The agent says which year a value is for.
* **Cost.** Each data query scans part of the WDI tables (BigQuery on-demand pricing; about a
  cent per question at list prices). Results are cached by BigQuery for 24 hours.

## Configuration (environment variables, all optional)

| Variable | Default | Purpose |
| --- | --- | --- |
| `WDI_MODEL` | `gemini-2.5-flash` | Gemini model on Vertex AI |
| `WDI_FALLBACK_MODEL` | `gemini-3-flash-preview` | Used if the first model is not found |
| `WDI_LOCATION` | `global` | Vertex AI location for the model |
| `WDI_DATASET` | `bigquery-public-data.world_bank_wdi` | Dataset with the WDI tables |
| `WDI_MAX_BYTES_BILLED` | 5 GiB | Per-query BigQuery byte limit |
| `GOOGLE_CLOUD_PROJECT` | project of the credentials | Project that runs and pays for queries |

## Inputs

* `query(message="What is the GDP of India?")` → `{"reply": ..., "tools_used": [...], "model": ...}`
* `input=`, `prompt=`, `question=` or `text=` work too.
* Optional `history=[{"role": "user"|"assistant", "content": "..."}]` for follow-up questions;
  the console's Chat sends it.
* JSON commands for debugging: `{"action": "list_tools"}` or
  `{"action": "call_tool", "name": "wdi_find_country", "arguments": {"name": "India"}}`.

## Local chat

Runs the agent on your computer with your own Google login (`gcloud auth application-default login`):

```bash
uv run --with-requirements wdi_agent/requirements.txt python tests/test_chat.py
```

## Tests

```bash
uv run --with-requirements wdi_agent/requirements.txt --with pytest python -m pytest tests/test_wdi_agent.py
```

They run offline with a scripted model and a fake BigQuery client.
