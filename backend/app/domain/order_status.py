"""İç sipariş statüleri ve geçiş kuralları.

Pazaryerlerinin ham statüleri (`orders.status`) her connector'da bu ortak
modele eşlenir (`orders.internal_status`).
"""
from dataclasses import dataclass

NEW = "new"
PREPARING = "preparing"
SENT_TO_SUPPLIER = "sent_to_supplier"
AWAITING_SHIPMENT = "awaiting_shipment"
SHIPPED = "shipped"
DELIVERED = "delivered"
CANCELLED = "cancelled"
RETURNED = "returned"
NEEDS_REVIEW = "needs_review"

LABELS_TR: dict[str, str] = {
    NEW: "Yeni",
    PREPARING: "Hazırlanıyor",
    SENT_TO_SUPPLIER: "Tedarikçiye Aktarıldı",
    AWAITING_SHIPMENT: "Kargoya Verilmeyi Bekliyor",
    SHIPPED: "Kargoda",
    DELIVERED: "Teslim Edildi",
    CANCELLED: "İptal",
    RETURNED: "İade",
    NEEDS_REVIEW: "Hata / İnceleme Gerekiyor",
}
ALL_STATUSES: tuple[str, ...] = tuple(LABELS_TR)

# Sipariş akışındaki ilerleme sırası. Terminal / istisna statüler sırada yok.
_PROGRESS = [NEW, PREPARING, SENT_TO_SUPPLIER, AWAITING_SHIPMENT, SHIPPED, DELIVERED]
TERMINAL = {DELIVERED, CANCELLED, RETURNED}

# Kullanıcının panelden yapabileceği manuel geçişler.
MANUAL_TRANSITIONS: dict[str, set[str]] = {
    NEW: {PREPARING, SENT_TO_SUPPLIER, CANCELLED, NEEDS_REVIEW},
    PREPARING: {SENT_TO_SUPPLIER, AWAITING_SHIPMENT, CANCELLED, NEEDS_REVIEW},
    SENT_TO_SUPPLIER: {AWAITING_SHIPMENT, CANCELLED, NEEDS_REVIEW},
    AWAITING_SHIPMENT: {SHIPPED, CANCELLED, NEEDS_REVIEW},
    SHIPPED: {DELIVERED, RETURNED, NEEDS_REVIEW},
    DELIVERED: {RETURNED, NEEDS_REVIEW},
    CANCELLED: {NEEDS_REVIEW},
    RETURNED: {NEEDS_REVIEW},
    NEEDS_REVIEW: {NEW, PREPARING, SENT_TO_SUPPLIER, AWAITING_SHIPMENT, SHIPPED,
                   DELIVERED, CANCELLED, RETURNED},
}


class InvalidTransition(ValueError):
    pass


def can_transition_manually(current: str, target: str) -> bool:
    return target in MANUAL_TRANSITIONS.get(current, set())


def check_manual_transition(current: str, target: str) -> None:
    if target not in LABELS_TR:
        raise InvalidTransition(f"Bilinmeyen statü: {target}")
    if not can_transition_manually(current, target):
        raise InvalidTransition(
            f"'{LABELS_TR.get(current, current)}' → '{LABELS_TR[target]}' geçişine izin verilmiyor"
        )


@dataclass(frozen=True)
class SyncDecision:
    status: str
    changed: bool


def resolve_sync_status(current: str | None, incoming: str) -> SyncDecision:
    """Pazaryerinden gelen statünün mevcut iç statüye uygulanıp uygulanmayacağı.

    * İlk kayıtta gelen statü aynen alınır.
    * İptal / iade / inceleme her zaman uygulanır (pazaryeri otoritedir).
    * İlerleme statülerinde geri gidilmez: ör. sipariş panelde
      "Tedarikçiye Aktarıldı" iken pazaryeri hâlâ "Hazırlanıyor" diyorsa
      iç statü korunur.
    * "İnceleme Gerekiyor" durumundaki sipariş, pazaryerinden ilerleme
      statüsü gelse de manuel olarak çözülene kadar incelemede kalır.
    """
    if current is None:
        return SyncDecision(incoming, True)
    if incoming == current:
        return SyncDecision(current, False)
    if incoming in (CANCELLED, RETURNED, NEEDS_REVIEW):
        return SyncDecision(incoming, True)
    if current == NEEDS_REVIEW:
        return SyncDecision(current, False)
    if current in (CANCELLED, RETURNED):
        # Pazaryeri iptal/iadeyi geri aldıysa bu olağan dışıdır -> inceleme.
        return SyncDecision(NEEDS_REVIEW, True)
    if current in _PROGRESS and incoming in _PROGRESS:
        if _PROGRESS.index(incoming) > _PROGRESS.index(current):
            return SyncDecision(incoming, True)
        return SyncDecision(current, False)
    return SyncDecision(incoming, True)
