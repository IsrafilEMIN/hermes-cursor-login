from providers import register_provider
from providers.base import ProviderProfile

cursor = ProviderProfile(
    name="cursor",
    aliases=("cursor-subscription", "cursor-native"),
    display_name="Cursor",
    description="Cursor subscription through a native authenticated bridge",
    signup_url="https://cursor.com",
    api_mode="chat_completions",
    env_vars=("CURSOR_BRIDGE_API_KEY",),
    base_url='http://127.0.0.1:8765/v1',
    auth_type="api_key",
    fallback_models=('default', 'composer-2.5', 'composer-2.5-fast', 'composer-2', 'composer-2-fast'),
    supports_health_check=True,
)

register_provider(cursor)
