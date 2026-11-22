"""Public display precision only; never feed formatted values into analysis."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from fractions import Fraction

from windows import require


_MAX_EXACT = 10**1000
_D_MAX_EXACT = Decimal(_MAX_EXACT)
_D_SUBUNIT_FLOOR = Decimal('0.001')
_F_SUBUNIT_FLOOR = Fraction(1, 1000)


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


_F_TENTH = Fraction(1, 10)
_F_HUNDREDTH = Fraction(1, 100)


def _subunit(value):
    sign = '-' if value < 0 else ''
    if isinstance(value, Fraction):
        abs_frac = abs(value)
        if abs_frac < _F_SUBUNIT_FLOOR:
            return '<0.001' if value > 0 else '>−0.001'
        scale = 100 if abs_frac >= _F_TENTH else (1000 if abs_frac >= _F_HUNDREDTH else 10000)
        scaled = abs_frac * scale
        q, r = divmod(scaled.numerator, scaled.denominator)
        rounded = q + (2 * r >= scaled.denominator)
        if rounded >= scale:
            return sign + '1'
        return sign + format((Decimal(rounded) / Decimal(scale)).normalize(), 'f')
    abs_dec = value.copy_abs()
    if abs_dec < _D_SUBUNIT_FLOOR:
        return '<0.001' if value > 0 else '>−0.001'
    return _format_subunit_decimal(abs_dec, sign)


def _format_subunit_decimal(abs_dec, sign):
    with localcontext() as ctx:
        ctx.prec = max(34, abs_dec.adjusted() + 10)
        quantum = Decimal((0, (1,), abs_dec.adjusted() - 1))
        rounded = abs_dec.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded >= 1:
            return sign + '1'
        return sign + format(rounded.normalize(), 'f')


def _check_boundaries(boundaries):
    validated = []
    for boundary in boundaries:
        parsed = _measurement(boundary, 'Invalid percentage boundary')
        require(0 <= parsed <= 100, 'Invalid percentage boundary')
        validated.append(parsed)
    return tuple(validated)


def number(value, grouping=True):
    """Whole units >= 1, 2-significant-digit decimals in [0.001, 1), and <0.001 floor."""
    if value is None:
        return 'Unavailable'
    value = _measurement(value)
    if not value:
        return '0'
    if -1 < value < 1:
        return _subunit(value)
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
