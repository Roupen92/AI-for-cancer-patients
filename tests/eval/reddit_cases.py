"""Ten realistic patient cases derived from the most common questions stem-cell /
bone-marrow transplant (HSCT) patients ask.

Reddit thread bodies are not reachable by the tooling crawler (reddit.com is
blocked), so the question themes were synthesized from the curated FAQ pages that
aggregate exactly these questions — most directly Fred Hutch's Long-Term Follow-Up
"Post-Transplant FAQ" (the most frequently asked questions from patients and
families), plus Dana-Farber, MD Anderson, NMDP/Be The Match, MSK, BMT InfoNet, and
OncoLink survivorship. The ten themes (fatigue, diet/neutropenic, GVHD, infection
precautions, re-vaccination, exercise, work/finances, fear of recurrence, taste/
mouth/eating, caregiver support) are the recurring high-frequency ones across all
of those sources and the matching subreddits (r/leukemia, r/lymphoma, r/AML,
r/multiplemyeloma, r/bonemarrowtransplants).

Locations deliberately span US / UK / Canada / Australia to exercise (a) the
Patient Navigator's country matching and (b) the cross-national institution path
("a US center says X, a UK charity says Y") that the institution-naming fix targets.

Each case is a dict the harness can feed straight into board.run_board().
"""

CASES = [
    {
        "id": "01_fatigue",
        "theme": "Fatigue / recovery timeline ('when will I feel normal again?')",
        "case": (
            "I'm 52, four months out from an allogeneic stem cell transplant (my sister "
            "was the donor) for acute myeloid leukemia (AML). Everyone said I'd bounce "
            "back, but I'm wiped out by lunchtime every single day and it's starting to "
            "scare me. Is this level of tiredness normal this far out? When does energy "
            "usually come back, and is there anything that actually helps the fatigue, or "
            "do I just have to wait it out? I want to start doing a little more but I'm "
            "afraid of setting myself back."
        ),
        "location": "Denver, Colorado, USA",
        "preferences": "I like walking outdoors; I don't have a gym.",
        "target_language": "English",
    },
    {
        "id": "02_diet_neutropenic",
        "theme": "Diet / 'neutropenic diet' / food safety / poor appetite",
        "case": (
            "I had a haploidentical stem cell transplant for AML about 8 weeks ago and "
            "I'm home now. I'm so confused about food. The hospital gave me a 'neutropenic "
            "diet' sheet, but people online say that advice is outdated. Can I eat fresh "
            "fruit and salad? What about takeaways? What is actually proven to lower "
            "infection risk versus just old rules nobody follows anymore? On top of that "
            "I've lost weight and have almost no appetite."
        ),
        "location": "Manchester, England, UK",
        "preferences": "I'm vegetarian.",
        "target_language": "English",
    },
    {
        "id": "03_gvhd_symptoms",
        "theme": "GVHD — what it is, symptoms, when to call the team",
        "case": (
            "About 100 days after my allogeneic transplant (unrelated donor) for acute "
            "lymphoblastic leukemia (ALL), I've developed a red, itchy rash on my hands "
            "and a really dry, sore mouth that makes eating hard. I've read this might be "
            "graft-versus-host disease. Can you explain what GVHD actually is in plain "
            "terms, what symptoms I should watch for, and how I tell the difference "
            "between something I should call the team about urgently versus mention at my "
            "next clinic visit?"
        ),
        "location": "Toronto, Ontario, Canada",
        "preferences": "",
        "target_language": "English",
    },
    {
        "id": "04_infection_precautions",
        "theme": "Infection risk / precautions / returning to the world",
        "case": (
            "I'm 60 and just home after an autologous stem cell transplant for multiple "
            "myeloma. I'm terrified of catching something. When is it safe to be around "
            "my grandkids, go to the supermarket, eat in a restaurant, or be near my dog? "
            "Do I need to wear a mask, and for how long? I feel like I'm living in a "
            "bubble and I don't know which precautions actually matter."
        ),
        "location": "Sydney, NSW, Australia",
        "preferences": "",
        "target_language": "English",
    },
    {
        "id": "05_vaccinations",
        "theme": "Re-vaccination / immune recovery / family vaccines",
        "case": (
            "It's been almost a year since my allogeneic stem cell transplant for "
            "myelodysplastic syndrome (MDS). My team mentioned I'll need to get all my "
            "childhood vaccines again. Why did I lose them? When does re-vaccination "
            "usually start and which ones come first? Is it safe for my kids and "
            "grandkids to get their normal vaccines around me, and should I get a flu "
            "shot this year?"
        ),
        "location": "Chicago, Illinois, USA",
        "preferences": "",
        "target_language": "English",
    },
    {
        "id": "06_exercise_strength",
        "theme": "Exercise / rebuilding strength safely",
        "case": (
            "Five months after my allogeneic transplant for AML I've lost so much muscle "
            "that I get breathless just going up the stairs. I want to rebuild my strength "
            "but I'm scared of hurting myself or picking up an infection at a gym. What "
            "kind of exercise is safe at this stage, how hard should I push, and how do I "
            "start without overdoing it? Is it even safe for me to lift weights yet?"
        ),
        "location": "London, England, UK",
        "preferences": "I have resistance bands at home; I have a bad left knee.",
        "target_language": "English",
    },
    {
        "id": "07_work_finances",
        "theme": "Returning to work / employment rights / financial help",
        "case": (
            "I'm 38, about 6 months out from an allogeneic stem cell transplant for ALL. "
            "My savings are almost gone and my employer is asking when I'm coming back, "
            "but I don't feel ready physically or mentally. What are my rights about "
            "taking more time off, what financial help exists for someone in my "
            "situation, and how do people figure out the right time to go back to work?"
        ),
        "location": "Austin, Texas, USA",
        "preferences": "",
        "target_language": "English",
    },
    {
        "id": "08_fear_of_recurrence",
        "theme": "Emotional — fear of relapse / scan anxiety / sleep",
        "case": (
            "I'm 18 months past my allogeneic transplant for lymphoma and physically I'm "
            "doing okay, but mentally I'm a mess. Every little ache makes me think it's "
            "back, I dread my checkup scans, and I can't sleep for the whole week before "
            "an appointment. I also feel guilty for struggling when I 'should' just be "
            "grateful I survived. Is this normal, and what actually helps with the fear "
            "of it coming back?"
        ),
        "location": "Vancouver, British Columbia, Canada",
        "preferences": "",
        "target_language": "English",
    },
    {
        "id": "09_taste_mouth_eating",
        "theme": "Taste changes / sore mouth / eating problems / weight loss",
        "case": (
            "Since my autologous transplant for myeloma three months ago, food tastes "
            "like metal or wet cardboard, my mouth gets sore, and I've lost about 7 kg. "
            "I force myself to eat but nothing appeals to me, and I'm worried about "
            "losing more weight and muscle. What can I do about the taste changes and the "
            "sore mouth, and how do I get enough nutrition when eating is this hard right "
            "now?"
        ),
        "location": "Birmingham, England, UK",
        "preferences": "No allergies.",
        "target_language": "English",
    },
    {
        "id": "10_caregiver",
        "theme": "Caregiver support / coping / what to have ready",
        "case": (
            "My husband had an allogeneic stem cell transplant for AML 7 weeks ago and is "
            "now home. I'm his main caregiver and I'm overwhelmed — managing his "
            "medications, watching for fevers, trying to cook safe food — and I'm "
            "exhausted and frightened myself. What support exists for caregivers like me, "
            "how do I look after my own mental health through this, and what should I have "
            "ready in case he spikes a fever?"
        ),
        "location": "Phoenix, Arizona, USA",
        "preferences": "",
        "target_language": "English",
    },
]
