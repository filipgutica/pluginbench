def grade(score, bonus):
    if score >= 90 and bonus:
        return "A"
    return "B" if score == 80 else "C"
