def col_sum(values):
    try:
        total = values[0]
        for v in values[1:]:
            total = total + v
        return total
    except TypeError:
        return "".join(str(v) for v in values)
