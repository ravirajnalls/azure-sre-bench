#!/usr/bin/env python3
"""Naive baseline agent: a generic guess that reads nothing (issue #4).

Prints the contract JSON without reading stdin, fixtures, or the environment —
its job is to fail. Running it through `bench run` proves every task requires
real investigation: a guess must score zero against specific rubrics.
"""

import json

GUESS = {
    "root_cause": "transient platform issue",
    "evidence": [],
    "fix": "restart and monitor",
}

if __name__ == "__main__":
    print(json.dumps(GUESS))
