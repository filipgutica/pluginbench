from __future__ import annotations


def cache_key(user_id: str, region: str) -> str:
    return f"{region.casefold()}:{user_id.casefold()}"


def store_profile(
    cache: dict[str, dict[str, str]], user_id: str, region: str, profile: dict[str, str]
) -> None:
    cache[cache_key(user_id, region)] = profile


def load_profile(
    cache: dict[str, dict[str, str]], user_id: str, region: str
) -> dict[str, str] | None:
    return cache.get(cache_key(user_id, region))
