"""Цена за блок по позиции.

Единственное место, где живёт цена, которую подставляем мы сами. Источник
заказа, приславший свою сумму, всегда в приоритете: Smartup присылает
`sold_amount`, и эта таблица на такие строки не влияет. Работает она там, где
суммы в источнике нет: телеграм-файлы, ручной заказ из бота, десктопный
разборщик.
"""

from .scan_quantities import product_key_from_name

# Цена за блок из 10 пачек, сум. В таблице только позиции, которые стоят не как
# все: формат KSSL идёт по 23 000 за пачку против 24 000 у OP и SSL 100.
# Подтверждение это цена самого Smartup по его заказам KSSL (imported_unit_price
# 23 000 против 24 000 у остальных форматов, снято с боевой базы 2026-09-21).
BLOCK_PRICE_BY_PRODUCT_KEY = {
    "brown:kssl": 230000,
    "green:kssl": 230000,
}


def block_price_for_product(product, default_price):
    """Цена за блок для позиции, default_price если позиции нет в таблице."""
    return BLOCK_PRICE_BY_PRODUCT_KEY.get(product_key_from_name(product), default_price)
