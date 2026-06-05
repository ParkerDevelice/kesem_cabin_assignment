# Camp Cabin Assigner

A Flask web app for generating cabin assignments for groups of campers.

## Features

- **Units** — Create groups of campers/counselors with headcounts
- **Cabins** — Define cabins with individual capacities
- **Cabin Groups** — Link 2+ cabins that can open into one larger space (combined capacity used during assignment)
- **Counselor Ratio** — Set the minimum required campers-per-counselor ratio; violations are flagged
- **Assignment Engine** — Greedy algorithm assigns each unit to the smallest available space that fits everyone; generates a detailed results report
- **Assignment History** — Save and revisit past assignment runs; printable report view

## Setup

```bash
cd cabin-assigner
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000

## How Assignment Works

1. Available "spaces" are built from standalone cabins **plus** any cabin groups (using their combined capacity).
2. Spaces are sorted by capacity (smallest first).
3. Each unit is matched to the smallest space that can hold all its campers **and** counselors.
4. Any unit whose counselor count is below `ceil(campers / ratio)` gets a warning — but still gets assigned if space exists.
5. Units with no fitting space are flagged as unassigned.

## Project Structure

```
cabin-assigner/
├── app.py              # Flask app, models, routes, assignment logic
├── requirements.txt
└── templates/
    ├── base.html       # Shared layout & styles
    ├── index.html      # Dashboard (units, cabins, groups, settings)
    └── assignment.html # Assignment results view
```

The SQLite database (`camp.db`) is created automatically on first run.
