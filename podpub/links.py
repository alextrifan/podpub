from __future__ import annotations

from .config import Config


def resolve_links(published: dict, config: Config) -> dict:
    """Збирає всі доступні посилання на епізод.

    Доступно одразу: Transistor share_url, YouTube URL.
    Deep links платформ додаються, лише якщо вже резолвнулись (TODO нижче);
    інакше — show-level фолбек із config.platforms.
    """
    links: dict[str, str] = {}
    if published.get("transistor_url"):
        links["Transistor"] = published["transistor_url"]
    if published.get("youtube_url"):
        links["YouTube"] = published["youtube_url"]

    # TODO: спробувати дістати deep links епізоду з Transistor distribution API.
    #       Якщо ще не резолвиться — використати show-level посилання з config.platforms.
    platforms = config.raw.get("platforms", {}) or {}
    for label, key in [
        ("Spotify", "spotify_show"),
        ("Apple Podcasts", "apple_show"),
        ("Amazon Music", "amazon_show"),
        ("Deezer", "deezer"),
    ]:
        deep_key = f"{label.lower().replace(' ', '_')}_url"
        if published.get(deep_key):
            links[label] = published[deep_key]
        elif platforms.get(key):
            links[label] = platforms[key]
    return links
