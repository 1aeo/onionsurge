"""Public display precision only; never feed formatted values into analysis."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction

from windows import require


_MAX_EXACT = 10**1000
_D_MAX_EXACT = Decimal(_MAX_EXACT)


def _measurement(value, message='Measurement must be numeric'):
    # Keep rational comparisons exact at percentage boundaries.
    if isinstance(value, Fraction):
        require(abs(value) <= _MAX_EXACT, 'Measurement exceeds supported range' if message == 'Measurement must be numeric' else message)
        return value
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(message) from error
    require(value.is_finite(), 'Measurement must be finite' if message == 'Measurement must be numeric' else message)
    require(value.copy_abs() <= _D_MAX_EXACT, 'Measurement exceeds supported range' if message == 'Measurement must be numeric' else message)
    return value


def _whole(value):
    if isinstance(value, Fraction):
        quotient, remainder = divmod(abs(value.numerator), value.denominator)
        rounded = quotient + (2 * remainder >= value.denominator)
        return -rounded if value < 0 else rounded
    # Unlike quantize(), integral rounding does not fail for large counters
    # or a caller's small Decimal context precision.
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def _check_boundaries(boundaries):
    validated = []
    for boundary in boundaries:
        parsed = _measurement(boundary, 'Invalid percentage boundary')
        require(0 <= parsed <= 100, 'Invalid percentage boundary')
        validated.append(parsed)
    return tuple(validated)


def number(value, grouping=True):
    """Whole units, with zero distinguished from a positive sub-unit value."""
    if value is None:
        return 'Unavailable'
    value = _measurement(value)
    if not value:
        return '0'
    if 0 < value < 1:
        return '<1'
    if -1 < value < 0:
        return '>−1'
    rounded = _whole(value)
    return f'{rounded:,}' if grouping else str(rounded)


def percentage(value, suffix=True, boundaries=(50, 100)):
    """Whole bounded shares without moving a value across a meaningful cutoff.

    Use boundaries=(90, 95, 100) for the measured-time distribution. Exact 100
    stays 100; a rounded non-total share is >99, not a false claim of unanimity.
    CPU utilization is unbounded and must use number(), not this function.
    """
    boundaries = _check_boundaries(boundaries)
    if value is None:
        return 'Unavailable'
    value = _measurement(value)
    if not 0 <= value <= 100:
        raise ValueError('Percentage must be between 0 and 100')
    rounded = _whole(value)
    if 0 < value < 1:
        label = '<1'
    elif value < 100 and rounded == 100:
        label = '>99'
    else:
        label = str(rounded)
        for boundary in boundaries:
            if rounded == boundary and value != boundary:
                # Values just above 95 are already in the 95–<100 bin, so 95 is
                # a faithful rounded display; strict-majority 50 is different.
                if value < boundary:
                    label = '<' + label
                elif boundary == 50:
                    label = '>' + label
                break
    return label + ('%' if suffix else '')
