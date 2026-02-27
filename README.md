# Fault Localization Prototype

A scalable fault localization system for Python and Java codebases.

## Pipeline

```
Stack trace + error log
    ↓
1. Static extraction (regex/parsing)
    ↓
2. Graph-based expansion (call graph)
    ↓
3. Hybrid retrieval (BM25 + dense)
    ↓
4. LLM re-rank + explain
```

## Setup

```bash
cd fault-localization
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

```python
from fault_localizer import FaultLocalizer

localizer = FaultLocalizer(codebase_path="./your-repo")
localizer.index()

results = localizer.localize("""
Traceback (most recent call last):
  File "app.py", line 42, in process
    return handler.execute()
ValueError: Invalid input
""")
```

## Project Structure

```
fault-localization/
├── extractors/        # Stack trace parsing (Python/Java)
├── indexer/           # Code indexing + embeddings
├── retrieval/         # BM25 + dense search
├── graph/             # Call graph analysis
├── ranker/            # LLM re-ranking
└── fault_localizer.py # Main orchestrator
```
