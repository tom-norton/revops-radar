#!/usr/bin/env python3
"""Tests for the pure, offline half of scan.py -- the scoring arithmetic, the hard
disqualifiers, the risk flags, and the location and title classifiers. No network, no API
key.

    python tests/test_scoring.py        (or: python -m pytest tests/)

These cover the logic that used to live in the prompt and drift silently: the weighted
total, which facts drop a role outright vs. which ones only get flagged on a scored row,
and the location gate that once accepted a Staines role and then let the model score it 2
for being outside the London commuter belt.

Three of these tests exist to keep old mistakes buried rather than to describe new
behaviour. `test_no_title_band_can_change_a_score` guards the removal of the cap engine,
which used to clamp a 6.5 RevOps role to 4.0 over one word in its title.
`test_include_title_admits_strategy_and_operations_wordings` guards the title gate against
re-narrowing, since Strategy & Operations is a core target and the market writes it a dozen
different ways. `test_language_and_salary_floor_are_not_score_flags` guards against those
two quietly turning back into flags on a scored row instead of the hard drop Tom asked for.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scan  # noqa: E402


def dims(experience=5, skills=5, seniority=5, domain=5, location_visa=5, trajectory=5):
    return {"experience": experience, "skills": skills, "seniority": seniority,
            "domain": domain, "location_visa": location_visa, "trajectory": trajectory}


NO_OBS = {"function_match": "core", "company_standout": True,
          "language_hard_requirement": False, "salary_stated": False,
          "salary_min_base": 0, "salary_max_base": 0, "salary_currency": ""}


def test_weighted_total():
    assert scan.weighted_total(dims()) == 5.0
    assert scan.weighted_total(dims(*([10] * 6))) == 10.0
    assert scan.weighted_total(dims(*([0] * 6))) == 0.0
    # weights must sum to 100, or every total is silently wrong
    assert sum(w for _, _, w, _ in scan.RUBRIC) == 100
    # a single dimension contributes exactly its weight
    assert scan.weighted_total(dims(experience=10, skills=0, seniority=0, domain=0,
                                    location_visa=0, trajectory=0)) == 2.5
    # missing keys are treated as 0 rather than blowing up
    assert scan.weighted_total({}) == 0.0
    # out-of-range input still clamps to the 0-10 scale
    assert scan.weighted_total(dims(*([99] * 6))) == 10.0


def test_cap_engine_is_gone():
    """The ceilings are not merely unused, they are absent. Left in place but unreferenced
    they would be reintroduced by the first person who greps for them."""
    for name in ["apply_caps", "below_visa_floor", "CAPS"]:
        assert not hasattr(scan, name), f"{name} is back; the score is being clamped again"


def test_no_title_band_can_change_a_score():
    """The regression this whole change exists to prevent. Every one of these titles used to
    be clamped -- analyst and specialist to 5.0, director+ to 6.0, deal desk to 4.0 -- which
    buried real roles under the dashboard gate on the strength of one word. The band is
    still computed, because it is worth flagging and worth telling the model about, but it
    must not touch the number."""
    strong = scan.weighted_total(dims(*([9] * 6)))
    for title in ["Senior Revenue Operations Analyst", "CS Operations Specialist",
                  "Sales Operations Coordinator", "Head of Revenue Operations",
                  "Director, GTM Operations", "Sales Strategy Associate Director",
                  "Deal Desk Manager", "Sales Strategy and Operations Associate, EMEA"]:
        job = {"title": title, "market": "UK-London"}
        out = scan.score_flags(job, NO_OBS)
        assert isinstance(out, list)                       # flags, never a score
        assert scan.weighted_total(dims(*([9] * 6))) == strong, title


def test_language_and_salary_floor_are_not_score_flags():
    """These two used to be flags on a scored row. Tom asked for them to work like
    says_no_sponsorship() / requires_other_language() instead: drop the role outright,
    don't score it, don't show it with a caveat attached. score_flags() must never mention
    either one -- see deep_score_disqualifier() for where they actually live now."""
    obs = dict(NO_OBS, language_hard_requirement=True, salary_stated=True,
               salary_min_base=40000, salary_currency="EUR")
    flags = scan.score_flags({"title": "Revenue Operations Manager", "market": "NL"}, obs)
    assert not any("english" in f.lower() or "fluency" in f.lower() for f in flags)
    assert not any("visa floor" in f for f in flags)


def test_deep_score_disqualifier_on_language():
    job = {"title": "Senior Customer Success Manager", "market": "NL"}
    stage, reason = scan.deep_score_disqualifier(job, dict(NO_OBS, language_hard_requirement=True))
    assert stage == "language-required"
    assert "non-English fluency" in reason
    # merely-preferred is the model's call to report as False, and then nothing disqualifies
    assert scan.deep_score_disqualifier(job, NO_OBS) == (None, None)


def test_deep_score_disqualifier_on_below_visa_floor():
    job = {"title": "Revenue Operations Manager", "market": "NL"}
    obs = dict(NO_OBS, salary_stated=True, salary_min_base=55000, salary_currency="EUR")
    stage, reason = scan.deep_score_disqualifier(job, obs)
    assert stage == "below-visa-floor"
    assert "below the NL visa floor" in reason
    # at or above the floor, nothing disqualifies
    obs = dict(NO_OBS, salary_stated=True, salary_min_base=85000, salary_currency="EUR")
    assert scan.deep_score_disqualifier(job, obs) == (None, None)


def test_a_range_is_judged_on_its_top_not_its_bottom():
    """The reason this check exists at all. Saga advertised 65,000-85,000 EUR in Amsterdam
    and Cockroach Labs 58,000-95,000 GBP in London; both were dropped on the bottom of a
    band whose top was comfortably above the visa floor. An employer can hire anywhere
    inside its own advertised range, so only a band that tops out below the floor is a
    role Tom genuinely cannot take."""
    nl = {"title": "Revenue Operations Expert", "market": "NL"}
    saga = dict(NO_OBS, salary_stated=True, salary_min_base=65000,
                salary_max_base=85000, salary_currency="EUR")
    assert scan.salary_floor_flag("NL", saga) == ""
    assert scan.deep_score_disqualifier(nl, saga) == (None, None)

    uk = {"title": "Strategy and Operations Manager, GTM - EMEA", "market": "UK-London"}
    cockroach = dict(NO_OBS, salary_stated=True, salary_min_base=58000,
                     salary_max_base=95000, salary_currency="GBP")
    assert scan.salary_floor_flag("UK-London", cockroach) == ""
    assert scan.deep_score_disqualifier(uk, cockroach) == (None, None)

    # A band whose TOP is still under the floor is the one that goes, and the note names
    # the whole band rather than a single number.
    doomed = dict(NO_OBS, salary_stated=True, salary_min_base=40000,
                  salary_max_base=52000, salary_currency="EUR")
    note = scan.salary_floor_flag("NL", doomed)
    assert "40000-52000 EUR" in note and "below the NL visa floor" in note
    assert scan.deep_score_disqualifier(nl, doomed)[0] == "below-visa-floor"


def test_a_single_stated_figure_is_its_own_top():
    """No range in the ad, and rows scored before salary_max_base existed, both arrive with
    no top. The figure that is there is the one to compare, which keeps the old, stricter
    reading for stale data rather than waving it through."""
    job = {"title": "Revenue Operations Manager", "market": "NL"}
    one = dict(NO_OBS, salary_stated=True, salary_min_base=55000,
               salary_max_base=55000, salary_currency="EUR")
    assert "stated salary 55000 EUR" in scan.salary_floor_flag("NL", one)
    stale = dict(NO_OBS, salary_stated=True, salary_min_base=55000, salary_currency="EUR")
    stale.pop("salary_max_base")
    assert "stated salary 55000 EUR" in scan.salary_floor_flag("NL", stale)
    assert scan.deep_score_disqualifier(job, stale)[0] == "below-visa-floor"
    # A top with no usable bottom is still a top, and still decides the call on its own.
    top_only = dict(NO_OBS, salary_stated=True, salary_min_base=0,
                    salary_max_base=95000, salary_currency="EUR")
    assert scan.salary_floor_flag("NL", top_only) == ""


def test_deep_score_disqualifier_language_wins_when_both_fire():
    """A role can only be dropped once. Language is checked first, so that's what gets
    logged when a posting is both a below-floor salary and a hard language requirement."""
    job = {"title": "Revenue Operations Manager", "market": "NL"}
    obs = dict(NO_OBS, language_hard_requirement=True, salary_stated=True,
               salary_min_base=40000, salary_currency="EUR")
    stage, _ = scan.deep_score_disqualifier(job, obs)
    assert stage == "language-required"


def test_the_scored_row_carries_the_whole_band():
    """applyq.comp_risk() decides whether to spend a research call on a role, and it can
    only tell a band that straddles the visa floor from one sitting under it if the row
    carries both ends. Nothing else re-reads the posting, so this is the only place the
    top of the band can come from."""
    job = {"title": "Revenue Operations Expert", "market": "NL"}
    data = {"dimensions": dims(), "function_match": "core", "company_standout": True,
            "language_hard_requirement": False, "salary_stated": True,
            "salary_min_base": 65000, "salary_max_base": 85000, "salary_currency": "EUR",
            "flags": [], "verdict": "worth applying"}
    res = scan.parse_score_result(job, data)
    assert not res.get("disqualified")
    assert res["comp"] == {"stated": True, "min_base": 65000.0, "max_base": 85000.0,
                           "currency": "EUR"}


def test_below_floor_never_compares_across_currencies():
    """55,000 GBP is below the 70,000 GBP UK floor but well above the 56,976 EUR Belgian
    one. Guessing an FX rate here would produce confident nonsense, so a mismatched
    currency must not disqualify the role at all."""
    obs = dict(NO_OBS, salary_stated=True, salary_min_base=55000, salary_currency="GBP")
    assert scan.salary_floor_flag("BE", obs) == ""
    assert "GBP" in scan.salary_floor_flag("UK-London", obs)
    # unparseable, absent or market-less salary never disqualifies either
    assert scan.salary_floor_flag("NL", dict(NO_OBS, salary_stated=True,
                                             salary_min_base="n/a",
                                             salary_max_base="n/a")) == ""
    assert scan.salary_floor_flag("NL", NO_OBS) == ""
    assert scan.salary_floor_flag(None, obs) == ""


def test_flag_off_target_function():
    obs = dict(NO_OBS, function_match="off_target")
    flags = scan.score_flags({"title": "Business Operations Manager", "market": "NL"}, obs)
    assert any("off-target" in f for f in flags)


def test_flag_title_band_reads_as_an_instruction_to_look():
    """Wording matters here: these bands were the ones being auto-buried, so the flag has to
    send Tom to the JD rather than deliver a verdict."""
    for title in ["Senior Revenue Operations Analyst", "CS Operations Specialist",
                  "Head of Revenue Operations"]:
        flags = scan.score_flags({"title": title, "market": "NL"}, NO_OBS)
        band = [f for f in flags if f.startswith("title band:")]
        assert band, title
        assert "check the JD" in band[0], title
    # an unremarkable title says nothing
    assert scan.score_flags({"title": "Revenue Operations Manager", "market": "NL"},
                            NO_OBS) == []


def test_flag_csm_track_by_market():
    """A Senior CSM role in NL is a primary target; the same role in London or Dublin gets a
    note unless the company is a standout (profile.md, CSM track weighting). It is a note
    now, not a ceiling -- the model reflects the weighting in the dimension scores."""
    title = "Senior Customer Success Manager"
    assert scan.score_flags({"title": title, "market": "NL"}, NO_OBS) == []
    plain = dict(NO_OBS, company_standout=False)
    for market in ["UK-London", "IE-Dublin", "BE"]:
        flags = scan.score_flags({"title": title, "market": market}, plain)
        assert any(market in f and "non-standout" in f for f in flags), market
        # a genuine standout gets no note
        assert scan.score_flags({"title": title, "market": market}, NO_OBS) == []
    # a RevOps title in those markets never picks up the CSM note
    assert scan.score_flags({"title": "Revenue Operations Manager",
                             "market": "UK-London"}, plain) == []


def test_several_flags_are_all_reported():
    """The old engine kept the lowest cap and discarded the rest of the reasoning. Flags
    accumulate instead, so nothing gets hidden behind whichever fact was worst. (Language and
    below-floor salary are excluded from this scenario deliberately -- they disqualify the
    role via deep_score_disqualifier() before score_flags() would ever run on it.)"""
    obs = dict(NO_OBS, company_standout=False)
    flags = scan.score_flags({"title": "Senior Customer Success Analyst",
                              "market": "UK-London"}, obs)
    assert len(flags) == 2      # title band, CSM outside NL


def test_market_of_uk_london_vs_rest_of_uk():
    assert scan.market_of("gb", "Staines, United Kingdom") == "UK-London"
    assert scan.market_of("gb", "London, England") == "UK-London"
    assert scan.market_of("gb", "Watford") == "UK-London"
    assert scan.market_of("gb", "") == "UK-London"            # bare GB feed row
    for city in ["Manchester", "Edinburgh", "Bristol", "Cambridge", "Leeds"]:
        assert scan.market_of("gb", city) is None, city
    # an unrecognised UK location is not assumed to be London
    assert scan.market_of("gb", "South East England") is None


def test_market_of_ireland_dublin_only():
    assert scan.market_of("ie", "Dublin") == "IE-Dublin"
    assert scan.market_of("ie", "Ireland") == "IE-Dublin"
    assert scan.market_of("", "Dublin, Ireland") == "IE-Dublin"
    for city in ["Cork, Ireland", "Galway", "Limerick"]:
        assert scan.market_of("ie", city) is None, city


def test_market_of_rejects_remote_and_off_target_countries():
    for loc in ["Remote", "Remote - EMEA", "Anywhere in Europe", "Work from home",
                "EMEA (Netherlands preferred)", "Ireland or Europe"]:
        assert scan.market_of("", loc) is None, loc
    # and the country field must not be a bypass -- this was the actual bug: REMOTE_ONLY
    # was only consulted on the country-less path
    assert scan.market_of("nl", "Remote") is None
    assert scan.market_of("be", "Remote, Europe") is None
    assert scan.market_of("gb", "Remote (UK)") is None
    for loc in ["Berlin, Germany", "Madrid, Spain", "Paris, France"]:
        assert scan.market_of("", loc) is None, loc


def test_market_of_named_city_beats_remote_wording():
    """A real Amsterdam job that mentions remote working is still an Amsterdam job."""
    assert scan.market_of("", "Amsterdam (remote-friendly)") == "NL"
    assert scan.market_of("", "Dublin or remote in Europe") == "IE-Dublin"
    assert scan.market_of("", "London, hybrid remote") == "UK-London"
    assert scan.market_of("", "Brussels, remote 2 days") == "BE"


def test_location_ok_agrees_with_market_of():
    for cc, loc in [("gb", "London"), ("gb", "Manchester"), ("nl", "Remote"),
                    ("", "Amsterdam"), ("", "Berlin"), ("ie", "Cork")]:
        assert scan.location_ok(cc, loc) == (scan.market_of(cc, loc) is not None)


def test_prefilter_returns_a_reason_not_a_bool():
    assert scan.prefilter("Revenue Operations Manager", "Amsterdam", "nl") is None
    assert "no target-function keyword" in scan.prefilter("Software Engineer", "Amsterdam", "nl")
    assert "excluded term" in scan.prefilter("VP Revenue Operations", "Amsterdam", "nl")
    assert "outside target markets" in scan.prefilter("Revenue Operations Manager",
                                                     "Berlin", "")
    # title is checked before location, so the reason names the first real problem
    assert "title" in scan.prefilter("Chef de Cuisine", "Berlin", "")


def test_include_title_is_word_order_agnostic_for_senior_csm():
    """The bug: a seniority qualifier only counted before "customer success", so moving it
    after the comma dropped the same job."""
    for a, b in [("Enterprise Customer Success Manager", "Customer Success Manager, Enterprise"),
                 ("Strategic Customer Success Manager", "Customer Success Manager, Strategic Accounts"),
                 ("Senior Customer Success Manager", "Customer Success Manager, Senior"),
                 ("Principal Customer Success Manager", "Customer Success Manager, Principal")]:
        assert scan.INCLUDE_TITLE.search(a), a
        assert scan.INCLUDE_TITLE.search(b), b


def test_include_title_admits_cs_team_lead_roles():
    """profile.md targets the Manager band, but every CS team-lead title was being dropped."""
    for t in ["Manager, Customer Success", "Manager, Customer Success Management",
              "Manager, Customer Success Managers, EMEA", "Manager, Customer Success, Scale EMEA",
              "Senior Manager, Customer Success", "Head of Customer Success"]:
        assert scan.INCLUDE_TITLE.search(t), t


def test_plain_csm_is_not_matched_by_the_keyword_list():
    """A plain CSM title must reach the market-conditional rule, not sneak in via the
    team-lead pattern -- otherwise the NL-only restriction is meaningless."""
    for t in ["Customer Success Manager", "Customer Success Manager - Denver",
              "Customer Success Manager II", "Customer Success Associate",
              "Scaled Customer Success Manager", "NA Customer Success Manager"]:
        assert not scan.INCLUDE_TITLE.search(t), t


def test_plain_csm_admitted_in_netherlands_only():
    assert scan.prefilter("Customer Success Manager", "Amsterdam", "nl") is None
    assert scan.prefilter("Customer Success Manager", "Netherlands", "nl") is None
    assert scan.prefilter("Customer Success Manager II", "Utrecht", "") is None
    for loc, cc in [("London", "gb"), ("Dublin", "ie"), ("Brussels", "be")]:
        reason = scan.prefilter("Customer Success Manager", loc, cc)
        assert reason and "title" in reason, f"{loc}: {reason}"
    # the NL carve-out must not become a bypass for genuinely off-target titles
    assert scan.prefilter("Software Engineer", "Amsterdam", "nl") is not None
    # and a senior CSM still passes everywhere, as before
    assert scan.prefilter("Senior Customer Success Manager", "London", "gb") is None


def test_include_title_covers_the_newly_added_revops_vocabulary():
    for t in ["Sales Compensation Manager", "Sales Compensation Design Lead",
              "Incentive Compensation Analyst", "Quota Planning Manager",
              "Territory Planning Manager", "Revenue Analytics Manager",
              "Revenue Systems Manager", "Revenue Technology Analyst",
              "Renewals Manager", "Renewals Specialist", "Strategy & Ops, Intercept",
              "Strategy and Operations Manager", "BizOps Manager", "Biz Ops Lead"]:
        assert scan.INCLUDE_TITLE.search(t), t


def test_widened_terms_stay_narrow_enough():
    """The additions must not drag in quota-carrying sales or unrelated ops roles."""
    for t in ["Territory Sales Director", "Senior Manager, Territory Sales",
              "Account Executive", "Business Development Representative",
              "Technical Account Manager", "People Business Partner",
              "Fraud Operations Manager", "Risk Operations Analyst",
              "Software Engineer", "Product Manager"]:
        assert not scan.INCLUDE_TITLE.search(t), t


def test_widening_did_not_lose_anything_previously_kept():
    """Regression guard. Every title the filter used to admit must still be admitted --
    widening a regex is an easy way to accidentally break an existing alternative."""
    previously_kept = [
        "Revenue Operations Manager", "RevOps Lead", "Rev Ops Manager",
        "Sales Operations Manager", "Sales Ops Analyst", "GTM Strategy Manager",
        "Go-to-Market Operations Manager", "Growth Operations Manager",
        "Marketing Operations Manager", "CS Operations Manager",
        "Customer Success Operations Manager", "Strategy and Operations Manager",
        "Strategy & Operations Manager", "Business Operations Manager",
        "Commercial Operations Manager", "Sales Strategy Manager",
        "Revenue Strategy Manager", "Revenue Enablement Manager",
        "Sales Enablement Manager", "Senior Customer Success Manager",
        "Principal Customer Success Manager, Enterprise", "Lead Customer Success - PropTech",
        "Enterprise Customer Success Manager", "Strategic Customer Success Manager",
    ]
    for t in previously_kept:
        assert scan.INCLUDE_TITLE.search(t), f"regression: {t} no longer matches"


def test_include_title_admits_strategy_and_operations_wordings():
    """Strategy & Operations is a core target function, and the market writes it a dozen
    ways. Every title here is a real posting the radar saw in one week and lost -- most died
    at the cheap screen, but the gate has to be wide enough that they reach it at all.
    Verkada's is the sharpest example: the skill names Verkada as a target employer and
    lists 'Strategy & Ops Associate at tier-1 employers' as viable."""
    for t in ["Sales Strategy and Operations Associate, EMEA",
              "Senior Analyst, Sales Strategy and Operations - Public Sector",
              "EMEA Partner Strategy and Operations Senior Manager",
              "Strategy and Operations Manager, gTech Agency and Partners",
              "International Strategy and Operations Lead",
              "Product Strategy and Operations Manager, Scaled Growth, EMEA",
              "Senior Strategy & Operations Manager, Prime Video Global Marketing",
              "Associate Director, Sales Planning Strategy and Operations",
              "Strategy, Planning & Operations Manager", "Strategic Operations Manager",
              "Business Strategy & Analytics Manager", "S&O Manager, EMEA",
              "EMEA Strategy Lead, AWS EMEA Sales Strategy",
              "GTM Systems Manager, Revenue Operations"]:
        assert scan.INCLUDE_TITLE.search(t), f"lost again: {t}"


def test_strategy_widening_did_not_admit_off_function_strategy_roles():
    """The other half of the same gate. These were all correctly dropped in the same week --
    'strategy' on its own is a very common word in titles that have nothing to do with
    revenue operations."""
    for t in ["Procurement External Talent Strategy Lead - EMEA",
              "Medical Strategy Lead, Oncology-Clinical Development",
              "Global Business Banking - Strategy Consultant (Digital Sales)",
              "Client Solution & Strategy Specialist (Institutional)",
              "Lead, Strategy", "Strategic Account Manager", "Contract Specialist",
              "Business Transformation Analyst", "Trade Analyst"]:
        assert not scan.INCLUDE_TITLE.search(t), f"over-wide: {t}"


def test_exclude_list_still_wins_over_the_widened_include():
    """Widening must not let an excluded seniority or an internship through."""
    for t in ["VP Revenue Operations", "Vice President, Sales Operations",
              "SVP Revenue Operations", "Sales Operations Intern",
              "Revenue Operations Internship", "Deal Desk Manager",
              "Working Student Sales Operations", "Renewals Manager Intern"]:
        assert scan.prefilter(t, "Amsterdam", "nl") is not None, t


def test_country_code_normalises_display_names():
    assert scan.country_code("Netherlands") == "nl"
    assert scan.country_code("The Netherlands") == "nl"
    assert scan.country_code("United Kingdom") == "gb"
    assert scan.country_code("Ireland") == "ie"
    assert scan.country_code("nl") == "nl"
    assert scan.country_code("Germany") == ""
    assert scan.country_code(None) == ""


def test_parse_date_loose_formats():
    from datetime import datetime, timezone
    assert scan.parse_date_loose("2026-07-20T10:00:00Z").year == 2026
    assert scan.parse_date_loose("20/07/2026") == datetime(2026, 7, 20, tzinfo=timezone.utc)
    assert scan.parse_date_loose(1753000000).year == 2025          # epoch seconds
    assert scan.parse_date_loose(1753000000000).year == 2025       # epoch millis
    for bad in [None, "", "not a date", "99/99/9999"]:
        assert scan.parse_date_loose(bad) is None, bad
    # naive ISO strings are treated as UTC rather than rejected
    assert scan.parse_date_loose("2026-07-20T10:00:00").tzinfo is not None


def test_recent_enough_fails_open_without_a_date():
    assert scan.recent_enough(None) is True
    assert scan.recent_enough("") is True
    assert scan.recent_enough("garbage") is True
    assert scan.recent_enough("2020-01-01T00:00:00Z") is False
    assert scan.recent_enough(scan.now_iso()) is True


def test_title_band_normal_titles_are_unremarkable():
    for title in ["Revenue Operations Manager", "Sales Operations Manager",
                  "GTM Strategy & Operations Manager", "Senior Customer Success Manager",
                  "Customer Success Operations Manager"]:
        assert scan.title_band(title) == "normal", title


def test_score_schema_covers_every_rubric_dimension():
    """The schema and the rubric have to stay in step, or a renamed dimension silently
    scores 0 for every job."""
    props = scan.SCORE_SCHEMA["properties"]["dimensions"]["properties"]
    assert set(props) == set(scan.RUBRIC_KEYS)
    assert set(scan.SCORE_SCHEMA["properties"]["dimensions"]["required"]) == set(scan.RUBRIC_KEYS)
    # the model must not be asked for a total -- that's computed here
    assert "score" not in scan.SCORE_SCHEMA["properties"]


def _same(a, b):
    return scan.same_role(scan.role_key(a), scan.role_key(b))


def _complete(j):
    return scan.role_key_complete(scan.role_key(j))


def test_dedupe_separates_dutch_cities():
    """The regression this exists for: the city bucket was a slice of the regex *pattern*,
    so every Dutch city hashed to the literal "amster" and the same role in Amsterdam and
    Rotterdam collapsed into a single dashboard entry."""
    ams = {"company": "Adyen", "title": "Revenue Operations Manager", "location": "Amsterdam"}
    rot = {"company": "Adyen", "title": "Revenue Operations Manager", "location": "Rotterdam"}
    utr = {"company": "Adyen", "title": "Revenue Operations Manager", "location": "Utrecht"}
    assert not _same(ams, rot)
    assert not _same(ams, utr)
    assert not _same(rot, utr)


def test_dedupe_collapses_genuine_duplicates():
    # same role, two sources, company written differently
    assert _same({"company": "Adyen N.V.", "title": "Revenue Operations Manager",
                  "location": "Amsterdam"},
                 {"company": "Adyen", "title": "Revenue Operations Manager",
                  "location": "Amsterdam, NL"})
    # The Hague's three spellings are one bucket
    hague = [{"company": "Adyen", "title": "Revenue Operations Manager", "location": loc}
             for loc in ("The Hague", "Den Haag", "Hague")]
    assert _same(hague[0], hague[1]) and _same(hague[1], hague[2])


def test_a_row_without_a_place_is_never_deduped():
    """role_key_complete() is the guard main() uses before comparing anything: a row with no
    company, no title or no identifiable place is never compared, so it can't swallow
    unrelated rows.

    A city outside DEDUPE_CITY used to leave the key incomplete too, which meant no row in
    Nijverdal, Delft or Staines was ever deduped against anything -- the live dashboard was
    carrying byte-identical pairs because of it. Such a city now buckets on its own name,
    which is what keeps Staines and Slough apart while still collapsing Staines twice. A
    location naming only a country stays incomplete: "Netherlands" must not become a bucket
    that two different Dutch cities fall into."""
    assert not _complete({"company": "", "title": "T", "location": "Amsterdam"})
    assert not _complete({"company": "X", "title": "", "location": "Amsterdam"})
    assert not _complete({"company": "X", "title": "T", "location": "Netherlands"})
    assert not _complete({"company": "X", "title": "T", "location": "United Kingdom"})
    assert not _complete({"company": "X", "title": "T", "location": "Remote - EMEA"})
    assert _complete({"company": "X", "title": "T", "location": "Amsterdam"})
    assert _complete({"company": "X", "title": "T", "location": "Groningen"})
    assert scan.dedupe_city("Staines, Surrey") == scan.dedupe_city("Staines-upon-Thames, England")
    assert scan.dedupe_city("Groningen") != scan.dedupe_city("Maastricht")


def test_same_role_collapses_a_shortened_company_name():
    """The Heidi case: revopsroles carried "Heidi Health", hiring.cafe carried "Heidi", and
    the exact-match key treated them as two employers -- so the same posting was screened
    and deep-scored twice and sat on the dashboard twice."""
    assert _same({"company": "Heidi Health", "title": "GTM Operations Analyst",
                  "location": "London, United Kingdom"},
                 {"company": "Heidi", "title": "GTM Operations Analyst",
                  "location": "London, London, United Kingdom"})
    # legal form, region and feed provenance are all noise on an employer name
    for other in ("Semrush UK Ltd.", "Semrush B.V.", "Semrush Job Board",
                  "Semrush, a DoorDash company"):
        assert _same({"company": "Semrush", "title": "Sales Operations Manager",
                      "location": "London"},
                     {"company": other, "title": "Sales Operations Manager",
                      "location": "London"}), other


def test_same_role_collapses_an_abbreviated_title():
    """The flatfair case: Adzuna's "Rev Ops Manager" and revopsroles' "Revenue Operations
    Manager", same company, same city, two dashboard rows and two Opus calls."""
    assert _same({"company": "flatfair", "title": "Revenue Operations Manager",
                  "location": "London, United Kingdom"},
                 {"company": "flatfair", "title": "Rev Ops Manager",
                  "location": "Somers Town, North West London"})
    same_title = ["Head of Sales Ops & Enablement", "Head of Sales Operations & Enablement",
                  "Head of Sales Operations and Enablement"]
    for t in same_title[1:]:
        assert _same({"company": "Altor", "title": same_title[0], "location": "London Area"},
                     {"company": "Altor", "title": t, "location": "London"}), t
    # word order is not identity: the same role gets written both ways round
    assert _same({"company": "Adyen", "title": "Manager, Sales Operations", "location": "Amsterdam"},
                 {"company": "Adyen", "title": "Sales Operations Manager", "location": "Amsterdam"})
    # a product or region suffix on one side only
    assert _same({"company": "IFS", "title": "Head of Revenue Operations", "location": "Staines, UK"},
                 {"company": "IFS", "title": "Head of Revenue Operations | IFS Copperleaf",
                  "location": "Staines-upon-Thames, England"})


def test_same_role_keeps_genuinely_different_postings_apart():
    """The other half of the trade. Loosening the match is only safe while these stay
    separate -- each pair is two real postings the live feeds carried at once, and merging
    any of them would hide a job rather than a duplicate."""
    def diff(a, b, why):
        assert not _same(a, b), why

    # seniority is never shortened away
    diff({"company": "Salesforce", "title": "Renewals Manager", "location": "Dublin"},
         {"company": "Salesforce", "title": "Senior Renewals Manager", "location": "Dublin"},
         "senior vs not")
    diff({"company": "Intercom", "title": "Senior Customer Success Manager", "location": "Dublin"},
         {"company": "Intercom", "title": "Principal Customer Success Manager, Enterprise",
          "location": "Dublin"}, "senior vs principal")
    # a language requirement makes it a different job -- and one of the two gets dropped
    # by requires_other_language() anyway, which it can't be if it was merged away first
    diff({"company": "MongoDB", "title": "Renewals Manager", "location": "Dublin"},
         {"company": "MongoDB", "title": "Renewals Manager - French Speaker", "location": "Dublin"},
         "French speaker")
    diff({"company": "Wise", "title": "Senior Customer Success Manager", "location": "London"},
         {"company": "Wise", "title": "Senior Customer Success Manager (German Speaking)",
          "location": "London"}, "German speaking")
    # so does a fixed term, and so does an experience band
    diff({"company": "LinkedIn", "title": "Sales Operations Associate", "location": "Dublin"},
         {"company": "LinkedIn", "title": "Sales Operations Associate (Fixed-Term Contract)",
          "location": "Dublin"}, "fixed-term")
    diff({"company": "Vega", "title": "Strategy & Operations (1-3 YoE)", "location": "London"},
         {"company": "Vega", "title": "Strategy & Operations (3-6 YoE)", "location": "London"},
         "years of experience")
    # different function, same company and city
    diff({"company": "Salesforce", "title": "Renewals Manager", "location": "Dublin"},
         {"company": "Salesforce", "title": "Manager, Quota and Capacity Planning",
          "location": "Dublin"}, "different function")
    # one company name containing another is not enough on its own
    diff({"company": "Zoom", "title": "Revenue Operations Manager", "location": "London"},
         {"company": "ZoomInfo", "title": "Revenue Operations Manager", "location": "London"},
         "Zoom vs ZoomInfo")
    # and the city still separates everything, which is what dkey was first fixed for
    diff({"company": "Adyen", "title": "Revenue Operations Manager", "location": "Amsterdam"},
         {"company": "Adyen", "title": "Revenue Operations Manager", "location": "Rotterdam"},
         "different city")


def test_same_role_collapses_a_bare_acronym():
    """The LSEG case: Adzuna carried the legal name, LinkedIn carried the acronym alone,
    and they share no word at all -- "lseg" is not a subset of {"london", "stock",
    "exchange"} or the reverse, so the ordinary company check never fires. Only the
    initials of the full name, "lseg", line up with the acronym."""
    assert _same({"company": "London Stock Exchange Group",
                  "title": "Revenue Operations Business Partner – Northern Europe",
                  "location": "London, UK"},
                 {"company": "LSEG",
                  "title": "Revenue Operations Business Partner – Northern Europe",
                  "location": "London, England, United Kingdom"})
    # the acronym still has to be the whole company on that side, not a word inside a
    # longer name that happens to start the same way
    diff = not _same({"company": "London Stock Exchange Group", "title": "T", "location": "London"},
                     {"company": "LSE Analytics", "title": "T", "location": "London"})
    assert diff


def test_same_role_does_not_merge_on_a_coincidental_acronym():
    """The flip side of the acronym check: it only closes the gap same_role() would
    otherwise leave for a genuine abbreviation, not license to merge on company alone.
    Two unrelated three-letter companies whose initials happen to line up with some other
    firm's name must still fail on title or city."""
    assert not _same({"company": "London Stock Exchange Group", "title": "Revenue Operations Manager",
                      "location": "London"},
                     {"company": "LSEG", "title": "Software Engineer", "location": "London"})
    assert not _same({"company": "London Stock Exchange Group", "title": "Revenue Operations Manager",
                      "location": "London"},
                     {"company": "LSEG", "title": "Revenue Operations Manager", "location": "Dublin"})


def test_group_duplicates_closes_a_non_transitive_chain():
    """The Amazon/AWS case, and the reason group_duplicates() exists instead of the single
    first-match pass dedupe used to do. "AWS" (Adzuna) and "Amazon" (hiring.cafe) don't
    match each other -- neither name's tokens contain the other's -- but both match
    LinkedIn's "Amazon Web Services (AWS)". A pass that stops at the first match a row
    finds pairs the full name with whichever partial one it meets first and never revisits
    the other, so the live dashboard carried "Amazon" and "Amazon Web Services (AWS)" as
    two separate rows for months after cross-source dedupe first shipped. Union-find closes
    the gap: both AWS~full and Amazon~full get discovered somewhere in the bucket, and
    unioning each onto the same root lands all three in one group even though AWS and
    Amazon are never compared directly."""
    title = "Business Operations Mgr, UKGI AWS SMGS Ops Sales Ops-WWPS"
    rows = [
        {"id": "az-1", "company": "AWS", "title": title, "location": "London, UK", "source": "adzuna"},
        {"id": "hc-1", "company": "Amazon", "title": title, "location": "London, UK", "source": "hiring.cafe"},
        {"id": "li-1", "company": "Amazon Web Services (AWS)", "title": title,
         "location": "London, UK", "source": "linkedin"},
    ]
    assert not _same(rows[0], rows[1])          # confirms this needs the transitive closure
    groups = scan.group_duplicates(rows)
    assert len(groups) == 1 and len(groups[0]) == 3
    kept, dropped = scan.collapse_duplicates(rows)
    assert len(kept) == 1 and len(dropped) == 2
    assert set(kept[0]["dupe_ids"]) == {"az-1", "li-1"} or set(kept[0]["dupe_ids"]) == {"hc-1", "li-1"} \
        or set(kept[0]["dupe_ids"]) == {"az-1", "hc-1"}


def test_group_duplicates_does_not_chain_through_an_unrelated_row():
    """The closure has to stop at same_role(), not run away with anything in the same city
    bucket. A fourth London row that matches neither AWS name must stay its own group."""
    title = "Business Operations Mgr, UKGI AWS SMGS Ops Sales Ops-WWPS"
    rows = [
        {"id": "az-1", "company": "AWS", "title": title, "location": "London, UK"},
        {"id": "li-1", "company": "Amazon Web Services (AWS)", "title": title, "location": "London, UK"},
        {"id": "az-2", "company": "Salesforce", "title": "Renewals Manager", "location": "London, UK"},
    ]
    groups = sorted(scan.group_duplicates(rows), key=len)
    assert [len(g) for g in groups] == [1, 2]


def test_merge_found_into_dashboard_never_promotes_a_fetch_over_an_existing_row():
    """Even a group where a fresh fetch would out-rank the dashboard row on prefer_row's
    own terms (a real score vs. none yet) must keep the dashboard row as the winner -- the
    whole point of this stage is that a job Tom has already been shown never gets
    rescored. Mixed with a same-run duplicate of the fetch, to confirm both the existing
    row and the extra fetch fold into the same winner."""
    existing = [{"id": "az-1", "company": "AWS",
                "title": "Business Operations Mgr, UKGI AWS SMGS Ops Sales Ops-WWPS",
                "location": "London, UK", "source": "adzuna", "score": 5.0}]
    found = [
        {"id": "hc-1", "company": "Amazon",
         "title": "Business Operations Mgr, UKGI AWS SMGS Ops Sales Ops-WWPS",
         "location": "London, UK", "source": "hiring.cafe"},
        {"id": "li-1", "company": "Amazon Web Services (AWS)",
         "title": "Business Operations Mgr, UKGI AWS SMGS Ops Sales Ops-WWPS",
         "location": "London, UK", "source": "linkedin"},
    ]
    kept_existing, kept_found, drops = scan.merge_found_into_dashboard(existing, found)
    assert kept_existing == [existing[0]]        # same object, not a fetch standing in for it
    assert kept_found == []                      # nothing left to screen or score
    assert len(drops) == 2
    assert set(existing[0]["dupe_ids"]) == {"hc-1", "li-1"}


def test_collapse_duplicates_keeps_the_best_copy_and_carries_the_ids():
    """A collapsed duplicate must not cost a Hide / Mark applied. The dashboard reads state
    against dupe_ids as well as the row's own id, so the surviving row has to carry them."""
    ats = {"id": "gh-1", "company": "Heidi Health", "title": "GTM Operations Analyst",
           "location": "London", "source": "greenhouse", "score": 7.0}
    agg = {"id": "az-2", "company": "Heidi", "title": "GTM Operations Analyst",
           "location": "London, London, United Kingdom", "source": "adzuna", "score": 6.5}
    kept, dropped = scan.collapse_duplicates([agg, ats])
    assert len(kept) == 1 and len(dropped) == 1
    assert kept[0]["id"] == "gh-1"              # higher score wins
    assert "az-2" in kept[0]["dupe_ids"]
    assert [a["source"] for a in kept[0]["also_seen"]] == ["adzuna"]
    # unscored rows fall back to the source: the employer's own feed over an aggregator
    for r in (ats, agg):
        r.pop("score", None), r.pop("dupe_ids", None), r.pop("also_seen", None)
    kept, _ = scan.collapse_duplicates([dict(agg), dict(ats)])
    assert kept[0]["id"] == "gh-1"


def test_collapse_duplicates_leaves_distinct_rows_alone():
    rows = [{"id": "1", "company": "Adyen", "title": "Revenue Operations Manager",
             "location": "Amsterdam"},
            {"id": "2", "company": "Adyen", "title": "Revenue Operations Manager",
             "location": "Rotterdam"},
            {"id": "3", "company": "Mollie", "title": "Revenue Operations Manager",
             "location": "Amsterdam"},
            {"id": "4", "company": "", "title": "Revenue Operations Manager",
             "location": "Amsterdam"},
            {"id": "5", "company": "", "title": "Revenue Operations Manager",
             "location": "Amsterdam"}]
    kept, dropped = scan.collapse_duplicates(rows)
    assert not dropped and len(kept) == 5        # rows 4 and 5 have no company: never merged


def test_gate_and_floor_are_ordered():
    assert scan.FLOOR < scan.GATE
    assert scan.NTFY_SCORE_THRESHOLD >= scan.GATE


def test_score_schema_uses_only_supported_json_schema_keywords():
    """Structured outputs reject numeric/length constraints and require additionalProperties
    false with everything listed in `required`. A schema that violates this 400s on the first
    real call, which is a slow way to find out."""
    UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                   "multipleOf", "minLength", "maxLength", "pattern", "minItems",
                   "maxItems", "uniqueItems", "$ref", "$defs", "allOf", "not"}

    def walk(node, path="root"):
        if not isinstance(node, dict):
            return
        bad = UNSUPPORTED & set(node)
        assert not bad, f"{path} uses unsupported keyword(s): {sorted(bad)}"
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, \
                f"{path} must set additionalProperties: false"
            props = node.get("properties", {})
            assert set(node.get("required", [])) == set(props), \
                f"{path}: every property must be required"
            for k, v in props.items():
                walk(v, f"{path}.{k}")
        if node.get("type") == "array":
            walk(node.get("items", {}), f"{path}[]")

    walk(scan.SCORE_SCHEMA)


def test_score_request_body_is_well_formed():
    """Build the exact body score_job() sends and assert the shape the API expects for
    claude-opus-5: cached system prefix, no `thinking` key (adaptive is the default, and
    disabling it is what makes this model leak reasoning into the answer), effort and the
    json_schema together under output_config, and enough max_tokens for thinking plus text."""
    import json as _json
    sent = {}

    def fake_post(url, timeout=None, headers=None, json=None):
        sent.update(json or {})
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"stop_reason": "end_turn", "usage": {},
                        "content": [{"type": "text", "text": _json.dumps({
                            "dimensions": {k: 7 for k in scan.RUBRIC_KEYS},
                            "function_match": "core", "company_standout": True,
                            "language_hard_requirement": False, "salary_stated": False,
                            "salary_min_base": 0, "salary_currency": "",
                            "flags": [], "verdict": "ok"})}]}
        return R()

    real_post = scan.requests.post
    scan.requests.post = fake_post
    try:
        out = scan.score_job("k", scan.score_system(), {
            "title": "Revenue Operations Manager", "company": "Adyen",
            "location": "Amsterdam", "market": "NL", "description": "d"})
    finally:
        scan.requests.post = real_post

    assert sent["model"] == "claude-opus-5"
    assert sent["max_tokens"] >= 2000, "thinking + response share max_tokens"
    assert "thinking" not in sent, "adaptive is the default on Opus 5; do not disable it"
    for k in ("temperature", "top_p", "top_k"):
        assert k not in sent, f"{k} is rejected on Opus 5"
    # system must be a block list carrying the cache breakpoint, not a bare string
    assert isinstance(sent["system"], list)
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "profile" in sent["system"][0]["text"].lower()
    oc = sent["output_config"]
    assert oc["effort"] in ("low", "medium", "high", "xhigh", "max")
    assert oc["format"]["type"] == "json_schema"
    assert oc["format"]["schema"] is scan.SCORE_SCHEMA
    # and the computed result carries the audit fields the dashboard renders
    assert out["score"] == 7.0 and out["score_raw"] == 7.0
    # score and score_raw are now always equal: nothing clamps the weighted total, and
    # score_raw survives only so rows written under the old cap engine still render.
    assert out["score"] == out["score_raw"]
    assert out["caps_applied"] == [] and out["tier"] == "NL"
    assert "comp not listed, verify vs floor" in out["flags"]


def test_score_job_returns_disqualified_instead_of_a_score():
    """score_job() must not silently produce a low score for a language-required or
    below-floor role -- it has to hand back the drop so the caller can route it to
    record_drop() instead of the dashboard."""
    import json as _json

    def fake_post(url, timeout=None, headers=None, json=None):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"stop_reason": "end_turn", "usage": {},
                        "content": [{"type": "text", "text": _json.dumps({
                            "dimensions": {k: 9 for k in scan.RUBRIC_KEYS},
                            "function_match": "core", "company_standout": True,
                            "language_hard_requirement": True, "salary_stated": False,
                            "salary_min_base": 0, "salary_currency": "",
                            "flags": [], "verdict": "great fit, wrong language"})}]}
        return R()

    real_post = scan.requests.post
    scan.requests.post = fake_post
    try:
        out = scan.score_job("k", scan.score_system(), {
            "title": "Senior Customer Success Manager", "company": "Wise",
            "location": "London", "market": "UK-London", "description": "d"})
    finally:
        scan.requests.post = real_post

    assert out == {"disqualified": True, "stage": "language-required",
                   "reason": scan.deep_score_disqualifier(
                       {"market": "UK-London"},
                       {"language_hard_requirement": True})[1]}
    assert "score" not in out


def test_claude_call_retries_transient_failures_then_gives_up():
    calls = {"n": 0}

    def make(status):
        def fake_post(url, timeout=None, headers=None, json=None):
            calls["n"] += 1
            class R:
                status_code = status
                text = "overloaded"
                def raise_for_status(self):
                    raise RuntimeError(f"HTTP {status}")
                def json(self):
                    return {"stop_reason": "end_turn", "usage": {},
                            "content": [{"type": "text", "text": "{}"}]}
            return R()
        return fake_post

    real_post, real_sleep = scan.requests.post, scan.time.sleep
    scan.time.sleep = lambda s: None       # don't actually wait through the backoff
    try:
        # 529 is retryable: all attempts used, then it raises
        scan.requests.post = make(529)
        raised = False
        try:
            scan._claude_call("k", "m", "s", "u", 100)
        except Exception:
            raised = True
        assert raised
        assert calls["n"] == scan.CLAUDE_ATTEMPTS, f"expected {scan.CLAUDE_ATTEMPTS} attempts"

        # 400 is a real bug, not a blip -- fail on the first attempt without retrying
        calls["n"] = 0
        scan.requests.post = make(400)
        try:
            scan._claude_call("k", "m", "s", "u", 100)
        except Exception:
            pass
        assert calls["n"] == 1, "4xx must not be retried"
    finally:
        scan.requests.post, scan.time.sleep = real_post, real_sleep


def test_score_job_raises_on_refusal_and_truncation():
    """Both used to arrive as an empty string and get stored as a real score of 0."""
    for stop in ("refusal", "max_tokens"):
        def fake_post(url, timeout=None, headers=None, json=None, _s=stop):
            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {"stop_reason": _s, "usage": {}, "content": []}
            return R()
        real_post = scan.requests.post
        scan.requests.post = fake_post
        try:
            raised = False
            try:
                scan.score_job("k", "sys", {"title": "T", "market": "NL"})
            except Exception:
                raised = True
            assert raised, f"stop_reason={stop} must raise, not score 0"
        finally:
            scan.requests.post = real_post


def test_clean_text_decodes_entities():
    assert scan.clean_text("Senior Manager Revenue Operations &amp; Systems") == \
        "Senior Manager Revenue Operations & Systems"
    assert scan.clean_text("Head&nbsp;of  RevOps\n") == "Head of RevOps"
    assert scan.clean_text("R&amp;D &lt;Lead&gt;") == "R&D <Lead>"
    assert scan.clean_text(None) == ""


def test_sponsor_register_matching():
    import sponsors
    reg = sponsors.Register("NL")
    reg.add("Adyen N.V.")
    reg.add("Booking.com B.V.")
    assert reg.match("Adyen") == "on_register"          # suffix stripped both sides
    assert reg.match("adyen n.v.") == "on_register"
    assert reg.match("Booking.com") == "on_register"
    assert reg.match("Some Random Startup") == "not_found"


def test_sponsor_register_withholds_negatives_when_partial():
    """A partial register must not answer "not a sponsor". This is the real failure mode:
    when the IND parse fails, the 5-line manual override file became the whole register and
    every other Dutch employer came back "not on register" -- a confident wrong answer that
    then cost the role points in the deep score."""
    import sponsors
    reg = sponsors.Register("NL")
    reg.add("Adyen N.V.")
    reg.trust_negatives = False
    assert reg.match("Adyen") == "on_register"     # hits still count
    assert reg.match("Mollie") == "unknown"        # misses do not
    reg.trust_negatives = True
    assert reg.match("Mollie") == "not_found"
    # an unmatchable company name is never a confident negative either
    assert sponsors.Register("UK").match("Ltd") == "unknown"


def test_sponsor_status_labels_cover_every_match_value():
    import sponsors
    for raw in ["on_register", "likely", "not_found", "unknown"]:
        assert sponsors.status_label(raw, "NL"), raw


# ------------------------------------------------------- hard disqualifiers
# The two facts that ended an application before it started and that run 1 missed entirely,
# because the scorer was handed a truncated or boilerplate copy of the ad. The first case in
# each list is the verbatim sentence from the posting that got through.

NO_SPONSOR_ADS = [
    "We’re not able to offer visa sponsorship or help with relocation support for this role.",
    "We do not sponsor work visas for this position.",
    "Unfortunately we are unable to provide visa sponsorship at this time.",
    "Please note: sponsorship is not available for this role.",
    "This role is not eligible for visa sponsorship.",
    "You must have the right to work in the UK without sponsorship.",
    "No visa sponsorship will be provided.",
    "We cannot offer sponsorship for this vacancy.",
]

SPONSOR_OK_ADS = [
    "Visa sponsorship is available for exceptional candidates.",
    "We are happy to sponsor visas and support relocation.",
    "We offer visa sponsorship and a relocation package.",
    "We have no restrictions on visa sponsorship for this role.",
    # Right to work alone is written by employers who DO sponsor -- it is not enough.
    "Applicants must have the right to work in the Netherlands.",
    "The company sponsors industry conferences and community events.",
    "This is a hybrid role. We sponsor Skilled Worker visas.",
]

LANG_REQUIRED_ADS = [
    "You are fluent in French, Dutch and English.",
    "Fluency in Dutch is required for this role.",
    "Native German speaker required.",
    "Business-level French is essential.",
    "You must speak Dutch and English.",
    "Dutch fluency is a must.",
]

LANG_OK_ADS = [
    # English is never a disqualifier -- he is a native speaker.
    "Fluency in English is required.",
    "Excellent written and verbal communication skills in English are essential.",
    "Dutch is a plus.",
    "German language skills are nice to have.",
    "French would be an advantage.",
    "Ideally you also speak Dutch.",
    "Experience selling into Spanish-speaking markets is preferred.",
    # "Requirements" must not read as "required"
    "Requirements: 5 years experience in the Dutch market.",
    "You will report to the French leadership team, based in Paris.",
]


def test_no_sponsorship_detected():
    for ad in NO_SPONSOR_ADS:
        assert scan.says_no_sponsorship(ad), ad


def test_no_sponsorship_not_triggered_by_an_employer_who_does_sponsor():
    for ad in SPONSOR_OK_ADS:
        assert not scan.says_no_sponsorship(ad), ad


def test_language_requirement_detected():
    for ad in LANG_REQUIRED_ADS:
        assert scan.requires_other_language(ad), ad


def test_language_requirement_not_triggered_by_a_preference():
    for ad in LANG_OK_ADS:
        assert not scan.requires_other_language(ad), ad


def test_disqualifiers_are_scoped_to_one_sentence():
    """A softener must only excuse its own sentence. Run 1's Edenred ad stated the hard
    requirement several bullets away from unrelated 'nice to have' wording."""
    ad = "German is a plus.\nYou are fluent in French, Dutch and English.\nStart date: January."
    assert scan.requires_other_language(ad) == "You are fluent in French, Dutch and English"
    # ...and a soft mention on its own still passes even next to other requirements.
    assert not scan.requires_other_language("Dutch is a plus.\n5 years of SaaS required.")


def test_language_under_a_preferred_heading_is_a_preference():
    """The qualifier lives in the section heading, on a different line from the bullet it
    qualifies. A real Stripe ad listing "Proficiency in Italian" under "Preferred
    qualifications" -- while stating those are "a bonus, not a requirement" -- was dropped
    as a hard Italian requirement until the section lookback existed."""
    ad = ("Minimum requirements\nHigh professional fluency in English.\n"
          "Preferred qualifications\nProficiency in Italian\n"
          "Experience with financial systems\n")
    assert not scan.requires_other_language(ad)
    # The nearest heading wins: a hard section after a soft one still counts.
    ad2 = ("Preferred qualifications\nExperience with Salesforce\n"
           "Minimum requirements\nFluency in Dutch\n")
    assert scan.requires_other_language(ad2) == "Fluency in Dutch"


def test_disqualifier_returns_the_quotable_sentence():
    """The drop log quotes the ad's own words, which is what makes a wrong drop reviewable."""
    quote = scan.says_no_sponsorship("About us. " + NO_SPONSOR_ADS[1] + " Apply now.")
    assert quote == "We do not sponsor work visas for this position"
    assert len(quote) <= 160


# ------------------------------------------------------- description capture

def test_adzuna_predicted_salary_is_not_forwarded():
    """salary_is_predicted=1 means Adzuna modelled the number; it is not in the ad. Two roles
    were capped to 4.0 on run 1 by a figure the employer never published."""
    assert scan.adzuna_salary({"salary_min": 44231, "salary_max": 44231,
                               "salary_is_predicted": "1"}, "gb") == ""
    assert scan.adzuna_salary({"salary_min": 44231, "salary_max": 52000,
                               "salary_is_predicted": "0"}, "gb") == "44231-52000 GBP"
    # A real ISO code, not "United Kingdom local" -- below_visa_floor() compares currencies.
    assert scan.adzuna_salary({"salary_min": 60000, "salary_max": 70000}, "nl") == "60000-70000 EUR"
    assert scan.adzuna_salary({}, "gb") == ""


def test_workday_urls_rewrite_to_the_json_api():
    """Workday renders in JavaScript, so a plain GET returns page furniture and no JSON-LD.
    Both tenant shapes expose the real posting through the same public CxS endpoint."""
    assert scan.workday_cxs_url(
        "https://kantar.wd3.myworkdayjobs.com/kantar/job/London-South-Bank-Central/X_R101983-1"
    ) == ("https://kantar.wd3.myworkdayjobs.com/wday/cxs/kantar/kantar/job/"
          "London-South-Bank-Central/X_R101983-1")
    assert scan.workday_cxs_url(
        "https://wd3.myworkdaysite.com/recruiting/edenpeople/Edenred_Careers/job/Brussels/Y_JR1"
    ) == ("https://wd3.myworkdaysite.com/wday/cxs/edenpeople/Edenred_Careers/job/Brussels/Y_JR1")
    # optional locale segment, and a query string, are both dropped
    assert scan.workday_cxs_url(
        "https://acme.wd1.myworkdayjobs.com/careers/en-US/job/Amsterdam/Z_R1?source=li"
    ) == "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/careers/job/Amsterdam/Z_R1"
    for not_workday in ["https://boards.greenhouse.io/mongodb/jobs/7941923",
                        "https://www.linkedin.com/jobs/view/123", "", "not a url"]:
        assert scan.workday_cxs_url(not_workday) == "", not_workday


def test_greenhouse_board_urls_rewrite_to_the_api():
    """Greenhouse board pages render client-side and carry no JSON-LD, so a posting linked
    by board URL rather than reached through the ATS feed came back empty."""
    assert scan.GREENHOUSE_BOARD_URL.match(
        "https://job-boards.greenhouse.io/purestorage/jobs/8075450").groups() \
        == ("purestorage", "8075450")
    assert scan.GREENHOUSE_BOARD_URL.match(
        "https://boards.greenhouse.io/liberis/jobs/8083222?gh_src=x").groups() \
        == ("liberis", "8083222")
    assert scan.greenhouse_board_desc("https://apply.workable.com/j/6F65F44B65") == ""
    assert scan.greenhouse_board_desc("") == ""


def test_sample_desc_keeps_both_ends():
    """A head-only slice threw away the closing block, which is where sponsorship terms,
    language requirements and comp are stated."""
    body = "START " + ("filler. " * 2000) + "END OF AD"
    assert len(body) > scan.DESC_CHAR_CAP
    out = scan.sample_desc(body)
    assert len(out) <= scan.DESC_CHAR_CAP
    assert out.startswith("START ")
    assert out.endswith("END OF AD")
    assert "[...]" in out
    # under the cap it is returned untouched
    assert scan.sample_desc("short ad") == "short ad"
    assert scan.sample_desc(None) == ""


def test_sample_desc_preserves_a_trailing_disqualifier():
    ad = ("We are hiring. " * 800) + "We are unable to offer visa sponsorship for this role."
    assert not scan.says_no_sponsorship(ad[:2200])          # the old head-only truncation
    assert scan.says_no_sponsorship(scan.sample_desc(ad))   # survives head+tail sampling


def test_strip_html_unescapes_before_stripping_tags():
    """Greenhouse returns escaped markup, so stripping first left nothing to strip and the
    unescape step then produced live <p> tags in the stored description."""
    assert "<p>" not in scan.strip_html("&lt;p&gt;As a Customer Success Manager&lt;/p&gt;")
    assert scan.strip_html("<p>Hello &amp; welcome</p>") == "Hello & welcome"


def test_strip_html_turns_block_tags_into_line_breaks():
    """Bullets carry no trailing punctuation, so without a break per item the list becomes
    one run-on sentence and a softener in one bullet excuses a requirement in another."""
    out = scan.strip_html("<ul><li>Dutch is a plus</li><li>You are fluent in French</li></ul>")
    assert "\n" in out
    assert scan.requires_other_language(out) == "You are fluent in French"


def _run():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  pass  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}: {e or '(assertion)'}")
        except Exception as e:
            failed.append(name)
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
