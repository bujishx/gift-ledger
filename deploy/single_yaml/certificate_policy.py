"""Leaf lifetimes for the single-YAML site's private certificate authority."""
from datetime import timedelta


LEAF_DAYS = 365
ACCEPTED_LIFETIMES = (timedelta(days=90), timedelta(days=LEAF_DAYS))


def renewal_start(moment, previous_start):
    return max(moment - timedelta(minutes=5), previous_start + timedelta(seconds=1))
