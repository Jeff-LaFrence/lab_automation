"""One-thread linear algebra, so seeded numerical results do not depend on the machine.

A threaded BLAS can sum in a different order when its thread count changes.
Most results do not notice, but a few steps amplify the last bits: Thompson
sampling from a Gaussian process draws through a factorization of a nearly
singular covariance, so the optimizer's picks changed with OPENBLAS_NUM_THREADS.
One thread makes that sampling slower (it covers every untested grid point),
but the same seed then gives the same picks on any thread count.
"""

from __future__ import annotations

from contextlib import AbstractContextManager

from threadpoolctl import threadpool_limits


def one_blas_thread() -> AbstractContextManager:
    """Run the BLAS calls inside the block on one thread."""
    return threadpool_limits(1, user_api="blas")
