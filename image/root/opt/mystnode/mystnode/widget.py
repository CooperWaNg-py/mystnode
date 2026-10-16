"""umbrelOS home-screen widget (`four-stats`).

umbreld requires EXACTLY 4 items and a non-empty `refresh` string.
"""

from decimal import ROUND_DOWN, Decimal, InvalidOperation

from .nodeclient import NodeAuthError, NodeClient, NodeHTTPError, NodeUnavailable

REFRESH = "30s"
DASH = "–"
USER_SERVICES = ("dvpn", "wireguard", "data_transfer", "scraping")
NODE_ERRORS = (NodeUnavailable, NodeAuthError, NodeHTTPError)


def _widget(items: list[dict]) -> dict:
    return {"type": "four-stats", "refresh": REFRESH, "link": "", "items": items}


def _placeholder(node_text: str, node_subtext: str) -> dict:
    return _widget([
        {"title": "Node", "text": node_text, "subtext": node_subtext},
        {"title": "Services", "text": DASH, "subtext": "running"},
        {"title": "Unsettled", "text": DASH, "subtext": "MYST"},
        {"title": "Lifetime", "text": DASH, "subtext": "MYST"},
    ])


def format_myst(value) -> str:
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, TypeError):
        return DASH


def build_widget(client: NodeClient, terms_accepted: bool) -> dict:
    if not terms_accepted:
        return _placeholder("Setup", "accept terms")
    try:
        health = client.get_json("/tequilapi/healthcheck", timeout=3)
    except NODE_ERRORS:
        return _placeholder("Offline", "")
    version = str(health.get("version", "")) if isinstance(health, dict) else ""

    services = DASH
    try:
        listed = client.get_json("/tequilapi/services?include_all=true")
        running = sum(
            1 for s in listed
            if isinstance(s, dict) and s.get("type") in USER_SERVICES and s.get("status") == "Running"
        )
        services = f"{running}/{len(USER_SERVICES)}"
    except NODE_ERRORS + (TypeError,):
        pass

    unsettled = lifetime = DASH
    try:
        identities = client.get_json("/tequilapi/identities").get("identities") or []
        if identities:
            identity = client.get_json(f"/tequilapi/identities/{identities[0]['id']}")
            unsettled = format_myst((identity.get("earnings_tokens") or {}).get("ether"))
            lifetime = format_myst((identity.get("earnings_total_tokens") or {}).get("ether"))
    except NODE_ERRORS + (AttributeError, KeyError, TypeError):
        pass

    return _widget([
        {"title": "Node", "text": "Online", "subtext": version},
        {"title": "Services", "text": services, "subtext": "running"},
        {"title": "Unsettled", "text": unsettled, "subtext": "MYST"},
        {"title": "Lifetime", "text": lifetime, "subtext": "MYST"},
    ])
