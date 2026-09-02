import unittest

from profile_cache import cache_key, load_profile, store_profile


class ProfileCacheTest(unittest.TestCase):
    def test_user_ids_remain_case_sensitive(self) -> None:
        self.assertNotEqual(cache_key("Casey", "US-WEST"), cache_key("casey", "us-west"))

    def test_profiles_do_not_collide(self) -> None:
        cache: dict[str, dict[str, str]] = {}
        store_profile(cache, "Casey", "US-WEST", {"name": "Casey A"})
        store_profile(cache, "casey", "us-west", {"name": "Casey B"})

        self.assertEqual(load_profile(cache, "Casey", "us-west"), {"name": "Casey A"})
        self.assertEqual(load_profile(cache, "casey", "US-WEST"), {"name": "Casey B"})


if __name__ == "__main__":
    unittest.main()
