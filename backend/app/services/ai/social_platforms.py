"""Sosyal medya yayın adaptörleri. Hiçbiri bağlı değil: kimlik bilgisi + yayın metodu uygulanmadan `connected()` False.

Bağlanacağı zaman: adaptörün `publish` metodu platformun döndürdüğü gönderi ID'sini dönmelidir; araç katmanı bu ID'yi
`ai_tool_calls.external_ref`'e yazar. ID yoksa yayın "yapıldı" sayılmaz.
"""
from __future__ import annotations

from ...config import get_settings, is_set


class SocialAdapter:
    code = "base"
    name = "?"
    PUBLISH_IMPLEMENTED = False

    def connected(self) -> bool:
        return False

    def not_connected_reason(self) -> str:
        return f"{self.name} bağlı değil"


class InstagramAdapter(SocialAdapter):
    code, name = "instagram", "Instagram (Graph API)"

    def connected(self) -> bool:
        s = get_settings()
        return bool(is_set(getattr(s, "meta_access_token", "")) and self.PUBLISH_IMPLEMENTED)

    def not_connected_reason(self) -> str:
        return ("Instagram Graph API yayın bağlantısı yok (META_ACCESS_TOKEN + instagram_content_publish izni ve yayın metodu "
                "uygulanmadı)")


class FacebookAdapter(InstagramAdapter):
    code, name = "facebook", "Facebook Sayfası (Graph API)"


class TikTokAdapter(SocialAdapter):
    code, name = "tiktok", "TikTok (Content Posting API)"

    def not_connected_reason(self) -> str:
        return "TikTok Content Posting API bağlantısı yok (uygulama onayı ve kimlik bilgisi yok)"


ADAPTERS = {"instagram": InstagramAdapter, "facebook": FacebookAdapter, "tiktok": TikTokAdapter}


def adapter_for(platform: str) -> SocialAdapter:
    return ADAPTERS.get(platform, SocialAdapter)()
