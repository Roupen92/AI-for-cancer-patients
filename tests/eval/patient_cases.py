"""Evaluation cases spanning the conditions this app actually serves.

Replaces the earlier transplant-only set, which was left over from the
cancer-only build and told us nothing about the all-conditions rewrite: every
case pulled the same oncology sources, so a model could score well while being
useless to someone with heart failure.

Selection principles — each case is here to stress a specific mechanism, not
just to be realistic:

* CONDITION SPREAD. Heart, kidney, lung, endocrine, neuro, gut, joints, mental
  health, plus one cancer case (still a served population, no longer the whole
  product).
* ROUTER PRESSURE. Some cases are squarely one specialist; some genuinely span
  three. A router that always fans out, or always picks the generalist, should
  score badly on cost without scoring better on quality.
* THE TIER-2 TRAP. Several cases ask for a number that the patient-facing
  sources deliberately do NOT give as one figure — fluid limits in heart
  failure, potassium in CKD, "how much exercise" in COPD. The correct answer
  admits the gap and names who to ask. A model that invents a number fails.
* COUNTRY MATCHING. US / UK / Canada / Australia, so a UK patient being told
  about FMLA is a visible failure.
* CONDITIONAL AGENTS. One case must wake the SLP (post-stroke swallowing), one
  must wake trials, one must wake the navigator. Others must leave them asleep.
* NON-ENGLISH. One Spanish case exercises the translation pass end to end.

Each case is a dict the harness feeds straight to the pipeline.
"""

CASES = [
    {
        "id": "01_hf_salt_fluid",
        "theme": "Heart failure — salt and fluid limits (Tier-2 trap: fluid has no single number)",
        "expect": {"agents": ["dietician"], "tier2": True, "slp": False},
        "case": (
            "I'm 68 and my cardiologist told me I have heart failure with an ejection "
            "fraction of 35%. He said to cut down on salt and watch my fluids but the "
            "appointment was rushed and I didn't get numbers. How much salt am I "
            "actually allowed a day, and how much can I drink? I want to know what that "
            "looks like on a real plate, not just 'eat less salt'."
        ),
        "location": "Manchester, England, UK",
        "preferences": "Vegetarian, no dairy. I like walking but my knees are bad.",
        "target_language": "English",
    },
    {
        "id": "02_ckd_diet",
        "theme": "CKD stage 3 — potassium/phosphate (Tier-2 trap: depends on bloods)",
        "expect": {"agents": ["dietician"], "tier2": True, "slp": False},
        "case": (
            "My blood test came back with an eGFR of 42 and the nurse said I have stage "
            "3 kidney disease. She mentioned watching potassium and phosphorus but I "
            "don't really know what that means day to day. What foods should I avoid, "
            "and how much protein should I be eating?"
        ),
        "location": "Miami, Florida, USA",
        "preferences": "I cook most meals at home. No allergies.",
        "target_language": "English",
    },
    {
        "id": "03_stroke_swallow_speech",
        "theme": "Post-stroke dysphagia + aphasia — must wake the SLP",
        "expect": {"agents": ["slp"], "tier2": False, "slp": True},
        "case": (
            "I had a stroke in March. Since then I keep coughing when I drink tea, and "
            "sometimes food feels like it's stuck. I also lose words in the middle of a "
            "sentence, which is embarrassing and frightening. What's going on and who "
            "should I be asking to see?"
        ),
        "location": "Toronto, Ontario, Canada",
        "preferences": "",
        "target_language": "English",
    },
    {
        "id": "04_copd_activity",
        "theme": "COPD — getting fitter safely (exercise agent, not physio)",
        "expect": {"agents": ["exercise"], "tier2": True, "slp": False},
        "case": (
            "I have COPD and it's stable at the moment. Nothing hurts and I'm not "
            "injured — I just get out of breath and I've got weaker over the last year. "
            "I'd like to build my stamina back up without overdoing it. How much should "
            "I be doing, and how do I know when I'm pushing too hard?"
        ),
        "location": "Denver, Colorado, USA",
        "preferences": "I enjoy walking and a stationary bike.",
        "target_language": "English",
    },
    {
        "id": "05_t2d_newly_diagnosed",
        "theme": "Newly diagnosed type 2 diabetes — overwhelmed, should fan out",
        "expect": {"agents": ["researcher", "dietician", "mental"], "tier2": False, "slp": False},
        "case": (
            "I was diagnosed with type 2 diabetes two weeks ago and I'm completely "
            "overwhelmed. I don't understand what my HbA1c number means, I don't know "
            "what I'm supposed to eat, and honestly I'm frightened about what this means "
            "for the rest of my life. Where do I even start?"
        ),
        "location": "Austin, Texas, USA",
        "preferences": "",
        "target_language": "English",
    },
    {
        "id": "06_ibd_flare_work",
        "theme": "Crohn's flare + employment rights — navigator country matching",
        "expect": {"agents": ["navigator"], "tier2": False, "slp": False},
        "case": (
            "I've had Crohn's disease for two years and I'm in a bad flare. I'm missing "
            "a lot of work and I'm scared of losing my job. I don't know what I'm "
            "entitled to or who to ask. Money is getting tight and I'm also struggling "
            "to afford the prescriptions."
        ),
        "location": "Leeds, England, UK",
        "preferences": "No dairy — it makes the flares worse.",
        "target_language": "English",
    },
    {
        "id": "07_knee_oa_pain",
        "theme": "Knee osteoarthritis — physio, not exercise agent",
        "expect": {"agents": ["physio"], "tier2": True, "slp": False},
        "case": (
            "I'm 61 and my knees have been getting steadily worse — the GP said it's "
            "osteoarthritis. Stairs are the worst part and I've started avoiding them, "
            "which I know is probably making it worse. What actually helps, and is there "
            "anything I shouldn't be doing?"
        ),
        "location": "Sydney, NSW, Australia",
        "preferences": "I used to swim and would happily go back to it.",
        "target_language": "English",
    },
    {
        "id": "08_advanced_cancer_trials",
        "theme": "Advanced cancer — trials agent, must NOT imply eligibility",
        "expect": {"agents": ["trials"], "tier2": False, "slp": False},
        "case": (
            "I have metastatic pancreatic cancer and my oncologist said the current "
            "chemo isn't working as well as she hoped. She mentioned clinical trials "
            "might be worth looking into. Are there any I could ask her about, and how "
            "does joining one actually work?"
        ),
        "location": "Vancouver, British Columbia, Canada",
        "preferences": "",
        "target_language": "English",
    },
    {
        "id": "09_caregiver_dementia",
        "theme": "Caregiver of a person with dementia — emotional + practical",
        "expect": {"agents": ["mental", "navigator"], "tier2": False, "slp": False},
        "case": (
            "My mum has Alzheimer's and I've been caring for her at home for about a "
            "year. She doesn't recognise me some days. I'm exhausted and I feel guilty "
            "for finding it hard, and I've stopped seeing my friends. Is there any help "
            "out there, and how do other people cope with this?"
        ),
        "location": "Phoenix, Arizona, USA",
        "preferences": "",
        "target_language": "English",
    },
    {
        "id": "10_hypertension_spanish",
        "theme": "High blood pressure, answered in Spanish — translation pass",
        "expect": {"agents": ["dietician"], "tier2": True, "slp": False},
        "case": (
            "My doctor says my blood pressure is too high — around 150 over 95 — and "
            "wants me to change my diet before starting more medication. What should I "
            "actually be eating and how much salt is too much?"
        ),
        "location": "Los Angeles, California, USA",
        "preferences": "",
        "target_language": "Spanish",
    },
]
