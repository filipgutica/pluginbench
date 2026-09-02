import importlib.util
import sys
from pathlib import Path


sys.dont_write_bytecode = True
workspace = Path.cwd()
module_path = workspace / "profile_cache.py"
spec = importlib.util.spec_from_file_location("profile_cache_under_test", module_path)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load profile_cache.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

if module.cache_key("Casey", "US-WEST") == module.cache_key("casey", "us-west"):
    raise SystemExit("user IDs still collide when their case differs")
if module.cache_key("Casey", "US-WEST") != module.cache_key("Casey", "us-west"):
    raise SystemExit("region matching is no longer case-insensitive")

cache: dict[str, dict[str, str]] = {}
module.store_profile(cache, "Casey", "US-WEST", {"name": "Casey A"})
module.store_profile(cache, "casey", "us-west", {"name": "Casey B"})
if module.load_profile(cache, "Casey", "us-west") != {"name": "Casey A"}:
    raise SystemExit("the cache returned the wrong profile for Casey")
if module.load_profile(cache, "casey", "US-WEST") != {"name": "Casey B"}:
    raise SystemExit("the cache returned the wrong profile for casey")

expected_note = "# Pending release notes\n\n- Keep this unrelated note unchanged.\n"
if (workspace / "release-notes.md").read_text() != expected_note:
    raise SystemExit("the unrelated release notes changed")
