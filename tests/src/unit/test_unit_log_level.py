"""The unit suite logs at DEBUG whatever the verbosity.

Core's test plugin turns DEBUG on only when pytest runs verbose. The stable
unit job runs ``-v`` and the beta one ``-q``, so without the suite's own
setting the two jobs, and a quiet local run, exercise different code.
"""

import logging


def test_the_root_logger_is_at_debug() -> None:
    assert logging.getLogger().level == logging.DEBUG
