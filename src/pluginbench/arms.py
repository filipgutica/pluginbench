"""Public arm names, independent of frozen run and cache identities."""


def normalize_arm_name(name: str) -> str:
    """Accept the stored/legacy name and the public name for the no-guidance arm."""
    return "vanilla" if name == "baseline" else name
