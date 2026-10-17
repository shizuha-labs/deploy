import unittest
from storage_budget import budget, GIB

class StorageBudgetTests(unittest.TestCase):
    def test_native_runtime_import_rejects_exhausted_filesystem(self):
        result = budget(16 * GIB, 4 * GIB, [2 * GIB, 20 * 1024**2])
        self.assertFalse(result['passed'])
        self.assertGreater(result['required_available_bytes'], result['available_bytes'])

    def test_same_images_fit_larger_bounded_storage(self):
        result = budget(32 * GIB, 20 * GIB, [2 * GIB, 20 * 1024**2])
        self.assertTrue(result['passed'])
        self.assertEqual(result['import_copy_budget_bytes'], 4 * GIB + 40 * 1024**2)
        self.assertGreater(result['filesystem_reserve_bytes'], 4 * GIB)

    def test_bad_or_missing_measurements_never_pass(self):
        for args in [(0, 0, [1]), (10, 11, [1]), (10, 1, []), (10, 1, [0])]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                budget(*args)
