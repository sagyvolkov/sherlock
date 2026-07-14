#!/usr/bin/env python3
"""
Sherlock v2 Operator — main entrypoint.

Starts the kopf operator and registers all benchmark Suite handlers.
Run with:
  python main.py          # normal operation
  python main.py --dev    # verbose / dev mode with instant reconcile
"""

import logging
import kopf

# Register all Suite handlers by importing them.
# kopf discovers handlers via decorators — modules must be imported here.
import src.handlers.pgbench      # noqa: F401
import src.handlers.hammerdb     # noqa: F401
import src.handlers.sysbench     # noqa: F401
import src.handlers.ycsb         # noqa: F401
import src.handlers.fio          # noqa: F401


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **kwargs):
    """Global operator settings."""
    # Retry failed handlers up to 5 times with exponential backoff
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = 'sherlock.io/operator-finalizer'

    # How often kopf re-checks objects that are in a waiting state
    settings.watching.server_timeout = 60
    settings.watching.client_timeout = 70


if __name__ == '__main__':
    kopf.run()
