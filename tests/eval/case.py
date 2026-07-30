"""Test case for the synthesis-quality eval loop: a post-haploidentical-HSCT
patient. Chosen to pull guidance from MULTIPLE institutions across countries
(US: ACS/NCCN/MSK/Fred Hutch/Be The Match; UK: Macmillan/CRUK/Anthony Nolan),
which is exactly the situation that produced the "MSK says X, the UK says Y,
and nobody told the patient what MSK is" failure mode.

SLP should pre-filter OUT (no head/neck/swallow involvement) — that's correct.
"""

CASE = (
    "I'm 47 and about 10 weeks ago I had a haploidentical stem cell transplant "
    "(a half-matched donor — my younger brother was the donor) for acute myeloid "
    "leukemia (AML). I'm home now. I get exhausted just walking to the kitchen and "
    "I've lost a lot of muscle and weight. I want to rebuild my strength safely but "
    "I'm scared of overdoing it. I'm also confused about food — I keep reading "
    "different things about food safety and the 'neutropenic diet' and I can't tell "
    "what's actually proven versus just old advice. My appetite is poor. On top of "
    "all that I'm anxious all the time that the leukemia will come back, and I'm "
    "barely sleeping. I also can't work right now and money is getting tight. "
    "What can I do?"
)

LOCATION = "Chicago, Illinois, USA"

PREFERENCES = (
    "I'd prefer walking or gentle exercise I can do at home — no gym access. "
    "I eat everything, no allergies."
)

TARGET_LANGUAGE = "English"
