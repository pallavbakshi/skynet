"""Settings validation regression tests."""

from __future__ import annotations

import unittest


class DefaultJobDeadlineCoercionTest(unittest.TestCase):
    """AGP_DEFAULT_JOB_DEADLINE_SECONDS <= 0 must coerce to None.

    Regression guard: a raw 0 would otherwise mean 'every job is born
    already past its deadline' and every dispatch would fail immediately.
    """

    def test_zero_coerces_to_none(self) -> None:
        from agp.config import Settings
        s = Settings(default_job_deadline_seconds=0)
        self.assertIsNone(s.default_job_deadline_seconds)

    def test_negative_coerces_to_none(self) -> None:
        from agp.config import Settings
        s = Settings(default_job_deadline_seconds=-1)
        self.assertIsNone(s.default_job_deadline_seconds)

    def test_positive_is_preserved(self) -> None:
        from agp.config import Settings
        s = Settings(default_job_deadline_seconds=1800)
        self.assertEqual(s.default_job_deadline_seconds, 1800)

    def test_none_is_preserved(self) -> None:
        from agp.config import Settings
        s = Settings(default_job_deadline_seconds=None)
        self.assertIsNone(s.default_job_deadline_seconds)

    def test_default_applies_when_unset(self) -> None:
        from agp.config import Settings
        s = Settings()
        self.assertEqual(s.default_job_deadline_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
