# WhatIf Engine

WhatIf Engine investigates the potential impact of software changes.

## Current milestone

- Discovers Python source files.
- Extracts imports using Python's AST parser.
- Reports syntax errors.
- Includes tests for the initial scanner.
- Does not execute scanned source code.

## Run

Activate the project's virtual environment, then run:

    PYTHONPATH=src python -m whatif_engine scan .

## Tests

    PYTHONPATH=src python -m unittest discover -s tests -v
