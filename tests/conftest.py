"""Shared test plumbing.

The ``calibratron`` logger (helpers.logger) deliberately does not propagate:
its own stdout handler is what the GUI tees into its log pane, and a root
handler would print every line twice. pytest's ``caplog`` only listens on the
root logger (pytest < 9), so a test asserting on what the bench *said* would
read an empty capture. For the duration of a test that asks for ``caplog``,
let the records propagate as well.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import helpers  # noqa: E402  (after the path insert on purpose)


@pytest.fixture(autouse=True)
def _calibratron_logger_reaches_caplog(request):
    if "caplog" not in request.fixturenames:
        yield
        return
    logger = helpers.logger
    previous = logger.propagate
    logger.propagate = True
    try:
        yield
    finally:
        logger.propagate = previous
