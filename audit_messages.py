"""Audit message builders for the receipt anomaly pipeline.

Pure functions that take numeric inputs and return user-facing
Tamil + Malay strings. Kept separate from ``bot.py`` so they can be
unit-tested without Telegram or Supabase side effects.
"""
from __future__ import annotations

# Below this many historical receipts the 14-day average is too noisy
# to cite confidently, so the message hedges with a "data masih sikit"
# disclaimer instead of presenting it as a settled baseline.
CONFIDENT_SAMPLE_SIZE = 5
LOW_CONFIDENCE_FLOOR = 3


def build_big_purchase_message(
    today_amount: float, avg: float, sample_size: int
) -> str:
    """Big-purchase audit message scaled by historical sample size.

    - ``sample_size <= 2``: question-only, no average. Reserved for
      future use — currently gated by the ``< 3`` guard in
      ``_check_big_purchase`` so this branch does not fire today.
    - ``sample_size`` 3-4: average shown with a "data masih sikit"
      disclaimer so the reader knows the comparison is weak.
    - ``sample_size >= 5``: confident 14-day average (original format).
    """
    prefix = "வாங்கினது அதிகம்! ஏன் இவ்வளவு வாங்கினீங்க? / "
    if sample_size < LOW_CONFIDENCE_FLOOR:
        return (
            f"{prefix}Belian besar hari ni RM{today_amount:.2f}. "
            "Apa sebab beli banyak? Stok habis? Event special? "
            "Atau pesanan customer?"
        )
    if sample_size < CONFIDENT_SAMPLE_SIZE:
        return (
            f"{prefix}Belian banyak hari ni! Kenapa beli lebih dari biasa? "
            f"(purata RM{avg:.2f} dari {sample_size} receipt sebelum, "
            f"data masih sikit. Hari ni RM{today_amount:.2f})"
        )
    return (
        f"{prefix}Belian banyak hari ni! Kenapa beli lebih dari biasa? "
        f"(purata 14 hari RM{avg:.2f}, hari ni RM{today_amount:.2f})"
    )
