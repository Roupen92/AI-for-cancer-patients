"""The single case used by `loop_eval capture` to freeze a synthesizer fixture.

Chosen to be hard in the ways this app fails:

* It spans FOUR specialties at once (movement, food, mood, money), so the
  synthesizer has to weave rather than concatenate.
* Its sources will span countries and bodies — the NHS, NICE, a US heart
  charity, a UK one — which is what produced the original "the UK says X, MSK
  says Y, and nobody told the patient who either is" failure.
* Two of the questions have no single published number (fluid limits, a weekly
  exercise target at this stage). The correct answer admits that and names who
  to ask. An answer that invents a figure is the failure the specificity gate
  exists to catch.
* It is deliberately NOT cancer. The previous fixture was a stem-cell transplant
  case, which meant every eval run exercised the oncology sources and told us
  nothing about the all-conditions rewrite.

SLP should pre-filter OUT — no swallow, voice or speech involvement. That is
correct behaviour, and worth checking on every capture.
"""

CASE = (
    "I'm 68 and I was told three weeks ago that I have heart failure — the letter "
    "says my ejection fraction is 35%. I'm still trying to take it in. I get out of "
    "breath putting the shopping away and my ankles swell by the evening. They told "
    "me to cut down on salt and watch my fluids but nobody gave me actual numbers, "
    "and I'm frightened of getting it wrong. I'd like to move more but I'm scared "
    "that pushing myself will damage my heart. I'm also not sleeping — I lie awake "
    "worrying about whether this is going to keep getting worse. And the "
    "prescriptions are adding up on a pension. What should I be doing?"
)

LOCATION = "Manchester, England, UK"

PREFERENCES = (
    "Vegetarian and I don't eat dairy. I like walking but my knees are bad, "
    "so nothing high-impact."
)

TARGET_LANGUAGE = "English"
