# CodeWise - AI-Powered Fault Localization

Find bugs in enterprise codebases using AI. Supports stack traces, screenshots, and natural language queries.

## Features

- **Stack Trace Analysis** - Parse Java/Python traces, find root causes via call graph
- **Natural Language Search** - "where do we handle pricing?" → relevant code
- **Image-Based Localization** - Screenshot of bug → suspected code locations
- **Solution Generation** - LLM analyzes candidates and suggests fixes
- **30M+ Scale** - OpenSearch backend for enterprise codebases

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Index a codebase
python cli.py /path/to/codebase --index-only

# Find fault from stack trace
python cli.py /path/to/codebase --error "NullPointerException at Service.java:45"

# Natural language search
python cli.py /path/to/codebase -q "where do we validate plan IDs?"

# Generate solution
python cli.py /path/to/codebase -q "pricing bug description" --solve
```

## Architecture

```
Input (stack trace / image / query)
    ↓
Extraction (parse frames, extract UI elements)
    ↓
Retrieval (BM25 + UniXcoder semantic search)
    ↓
Graph Expansion (find callers = root causes)
    ↓
LLM Re-ranking (Claude Sonnet)
    ↓
Solution Generation (optional)
```

## Models Used

| Component | Model |
|-----------|-------|
| Embeddings | UniXcoder (microsoft/unixcoder-base) |
| LLM | Claude 3.5 Sonnet (Bedrock) |
| Vision | Claude 3.5 Sonnet (Bedrock) |

## CLI Options

```
python cli.py <codebase> [options]

Options:
  -e, --error TEXT      Stack trace or error message
  -f, --error-file FILE File containing stack trace
  -q, --query TEXT      Natural language query
  -k, --top-k N         Number of results (default: 5)
  -s, --solve           Generate solution with LLM
  --no-llm              Skip LLM re-ranking
  --index-only          Only index, don't search
  --force-reindex       Force re-indexing
```

## Production Deployment

See [SETUP.md](SETUP.md) for AWS deployment with OpenSearch.

## Project Structure

```
fault-localization/
├── cli.py                 # Command-line interface
├── fault_localizer.py     # Core engine
├── extractors/            # Stack trace & image parsing
├── indexer/               # Code parsing & embedding
├── retrieval/             # BM25 + semantic search
├── graph/                 # Call graph analysis
├── ranker/                # LLM ranking & solutions
├── storage/               # OpenSearch backend
└── infra/                 # CDK deployment
```
