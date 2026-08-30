"""Intentional Fast Checks smoke-test violations for #2311. Never merge."""

import os
import sys


def smoke() -> int:
    value: int = "not an int"
    return value


BAD_FORMAT = {  'spacing' : 1,   'quotes'  :2 }
