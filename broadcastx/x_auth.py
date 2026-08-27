"""Shared, value-free checks for X browser authentication state."""


def has_auth_cookies(cookies: list[dict]) -> bool:
    names = {cookie.get("name") for cookie in cookies}
    return "auth_token" in names and "ct0" in names


def looks_logged_out(text: str) -> bool:
    lowered = text.lower()
    logged_out_markers = (
        "happening now",
        "email or username",
        "new to x?",
        "sign up now",
        "don't miss what's happening",
    )
    auth_actions = ("log in", "sign up", "create account", "continue with")
    return any(marker in lowered for marker in logged_out_markers) and any(
        action in lowered for action in auth_actions
    )
