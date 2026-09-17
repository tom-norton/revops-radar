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

import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scan  # noqa: E402


def dims(experience=5, skills=5, seniority=5, domain=5, location_visa=5, trajectory=5):
    return {"experience": experience, "skills": skills, "seniority": seniority,
            "domain": domain, "location_visa": location_visa, "trajectory": trajectory}


NO_OBS = {"function_match": "core", "company_standout": True,
          "language_hard_requirement": False, "salary_stated": False,
          "salary_min_base": 0, "salary_currency": ""}


def flag_job(**kw):
    """A job dict for score_flags() tests, carrying a real-length description.

    The description matters even when a test is about something else: score_flags() flags
    thin evidence, so a fixture with no description picks that up and every assertion about
    flag counts drifts by one."""
    job = {"title": "Revenue Operations Manager", "market": "NL",
           "description": "x" * (scan.MIN_DESC_CHARS + 1)}
    job.update(kw)
    return job


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


def test_deep_score_disqualifier_language_wins_when_both_fire():
    """A role can only be dropped once. Language is checked first, so that's what gets
    logged when a posting is both a below-floor salary and a hard language requirement."""
    job = {"title": "Revenue Operations Manager", "market": "NL"}
    obs = dict(NO_OBS, language_hard_requirement=True, salary_stated=True,
               salary_min_base=40000, salary_currency="EUR")
    stage, _ = scan.deep_score_disqualifier(job, obs)
    assert stage == "language-required"


def test_below_floor_never_compares_across_currencies():
    """55,000 GBP is below the 70,000 GBP UK floor but well above the 56,976 EUR Belgian
    one. Guessing an FX rate here would produce confident nonsense, so a mismatched
    currency must not disqualify the role at all."""
    obs = dict(NO_OBS, salary_stated=True, salary_min_base=55000, salary_currency="GBP")
    assert scan.salary_floor_flag("BE", obs) == ""
    assert "GBP" in scan.salary_floor_flag("UK-London", obs)
    # unparseable, absent or market-less salary never disqualifies either
    assert scan.salary_floor_flag("NL", dict(NO_OBS, salary_stated=True,
                                             salary_min_base="n/a")) == ""
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
        flags = scan.score_flags(flag_job(title=title), NO_OBS)
        band = [f for f in flags if f.startswith("title band:")]
        assert band, title
        assert "check the JD" in band[0], title
    # an unremarkable title says nothing
    assert scan.score_flags(flag_job(title="Revenue Operations Manager"), NO_OBS) == []


def test_flag_csm_track_by_market():
    """A Senior CSM role in NL is a primary target; the same role in London or Dublin gets a
    note unless the company is a standout (profile.md, CSM track weighting). It is a note
    now, not a ceiling -- the model reflects the weighting in the dimension scores."""
    title = "Senior Customer Success Manager"
    assert scan.score_flags(flag_job(title=title), NO_OBS) == []
    plain = dict(NO_OBS, company_standout=False)
    for market in ["UK-London", "IE", "BE"]:
        flags = scan.score_flags(flag_job(title=title, market=market), plain)
        assert any(market in f and "non-standout" in f for f in flags), market
        # a genuine standout gets no note
        assert scan.score_flags(flag_job(title=title, market=market), NO_OBS) == []
    # a RevOps title in those markets never picks up the CSM note
    assert scan.score_flags(flag_job(title="Revenue Operations Manager",
                                     market="UK-London"), plain) == []


def test_several_flags_are_all_reported():
    """The old engine kept the lowest cap and discarded the rest of the reasoning. Flags
    accumulate instead, so nothing gets hidden behind whichever fact was worst. (Language and
    below-floor salary are excluded from this scenario deliberately -- they disqualify the
    role via deep_score_disqualifier() before score_flags() would ever run on it.)"""
    obs = dict(NO_OBS, company_standout=False)
    flags = scan.score_flags(flag_job(title="Senior Customer Success Analyst",
                                      market="UK-London"), obs)
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


def test_market_of_covers_all_of_ireland_not_just_dublin():
    """Ireland is in scope country-wide. This used to be a Dublin-only gate, with an
    IE_OTHER_CITY regex that rejected Cork, Galway, Limerick and Waterford outright; the
    drop log shows "Burnfoot, County Donegal" and "Dunshaughlin, County Meath" being
    thrown away as "not Dublin commuter". Outside Dublin the cost of living is lower
    against the same permit threshold, so these are better outcomes, not worse."""
    assert scan.market_of("ie", "Dublin") == "IE"
    assert scan.market_of("ie", "Ireland") == "IE"
    assert scan.market_of("", "Dublin, Ireland") == "IE"
    for loc in ["Cork, Ireland", "Galway", "Limerick", "Waterford", "Letterkenny",
                "Dun Laoghaire, Ireland", "Dublin 8, County Dublin, Ireland",
                "Dublin, Leinster, Ireland", "County Clare",
                "Burnfoot, County Donegal, Ireland",
                "Dunshaughlin, County Meath, Ireland"]:
        assert scan.market_of("", loc) == "IE", loc


def test_irish_county_names_that_are_also_british_places_do_not_leak():
    """Louth, Clare, Bray, Meath and Mayo are Irish counties and also British place names.
    A bare match on those would do two wrong things at once: route a UK row to Ireland,
    and bypass the London-only rule, because UK_OTHER_CITY does not list them. So a county
    only counts with the "County"/"Co." prefix the Irish feeds actually emit."""
    for loc in ["Louth, Lincolnshire, UK", "Clare, Suffolk, UK", "Bray, Wiltshire"]:
        assert scan.market_of("", loc) is None, loc
    # a commuter-belt row keeps its own market rather than being pulled to Ireland
    assert scan.market_of("", "Mayo, Kent") == "UK-London"
    # ...and with the prefix, they resolve
    for loc in ["County Louth", "Co. Clare, Ireland", "County Mayo"]:
        assert scan.market_of("", loc) == "IE", loc


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
    assert scan.market_of("", "Dublin or remote in Europe") == "IE"
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
    # Raised from 6.0/5.0 when the map went from four markets to six. The previous 45 days
    # of 478 scored rows sat 233 above 6.0 and 156 above 6.5, so this is about 3.5 "apply"
    # roles a day rather than five.
    assert (scan.GATE, scan.FLOOR) == (6.5, 6.0)


def test_only_the_wanted_markets_ring_the_phone():
    """A push notification is a claim on Tom's attention right now, not an entry on a list
    he reads when he chooses. A US role can score 8 on the strength of the role itself
    while still being somewhere he does not want to move, so it stays on the dashboard and
    does not ring."""
    assert scan.NTFY_MAX_TIER == 2
    ring = [m for m, t in scan.MARKET_TIER.items() if t <= scan.NTFY_MAX_TIER]
    assert set(ring) == {"NL", "IE", "UK-London"}
    for market in ("CA", "US-Remote", "BE"):
        assert scan.market_tier(market) > scan.NTFY_MAX_TIER, market


def test_the_dashboard_is_handed_the_market_ordering_rather_than_keeping_a_copy():
    """docs/index.html sorts by tier then score, and reads the tier table out of
    status.json for the same reason it reads gate and floor from there: a second copy in
    the page is a copy that drifts."""
    page = open(os.path.join(os.path.dirname(__file__), "..", "docs", "index.html"),
                encoding="utf-8").read()
    assert "allStatus.market_tier" in page
    assert "tierOf(a) - tierOf(b)" in page
    # an unknown market must sort LAST, or a market added to scan.py before the dashboard
    # has seen it jumps straight to the top of the list
    assert "TIER_UNKNOWN = 9" in page
    assert "?? TIER_UNKNOWN" in page


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


def test_redact_removes_a_token_from_a_requests_exception_message():
    """The failure that stopped the radar for three days: an Apify 429 put the request URL,
    token and all, into the exception text, which was written into docs/status.json and
    pushed. GitHub push protection rejected the push and every scan after it died at the
    same step."""
    os.environ["APIFY_API_TOKEN"] = "apify_api_pretendvalue1234567890"
    try:
        msg = ("FAIL: 429 Client Error: Too Many Requests for url: "
               "https://api.apify.com/v2/acts/memo23~apify-hiring-cafe-scraper/"
               "run-sync-get-dataset-items?token=apify_api_pretendvalue1234567890")
        out = scan.redact(msg)
        assert "apify_api_pretendvalue1234567890" not in out
        assert "429 Client Error" in out, "the diagnosis has to survive the redaction"
    finally:
        del os.environ["APIFY_API_TOKEN"]


def test_redact_scrubs_credential_query_params_it_has_no_env_value_for():
    """A token this process never held -- one Adzuna or a job source hands back inside a
    URL -- is still a secret GitHub will block the push over."""
    out = scan.redact("error: connection failed for url: "
                      "https://api.adzuna.com/v1/api/jobs/gb/search/1?app_id=abc123&app_key=deadbeefcafe&what=revops")
    assert "deadbeefcafe" not in out
    assert "abc123" not in out
    assert "what=revops" in out, "non-credential params carry the diagnosis and stay"


def test_redact_leaves_an_ordinary_status_line_alone():
    """A footer full of <redacted> would hide the thing the footer exists to show."""
    line = "ok (raw 98 -> kept 82) | revops-broad=raw 30; cs-nl=raw 27"
    assert scan.redact(line) == line


def test_apify_call_keeps_the_token_out_of_the_url():
    """Guards the call site itself. Header auth is what stops the exception text from ever
    containing the token; redact() is the backstop, not the fix."""
    import inspect
    src = inspect.getsource(scan.fetch_apify_hiringcafe)
    assert '"Authorization": f"Bearer {token}"' in src
    assert 'params={"token": token}' not in src


# ---- North America: Canada and the US


def test_canada_resolves_anywhere_in_the_country():
    """Tom is a Canadian citizen by descent, so no part of Canada is out of scope and a
    remote-in-Canada role counts too."""
    for loc in ["Toronto, ON", "Vancouver, British Columbia", "Montreal, QC, Canada",
                "Ottawa, Ontario", "Calgary, AB", "Remote - Canada", "Halifax, NS"]:
        assert scan.market_of("", loc) == "CA", loc
    assert scan.market_of("ca", "") == "CA"


def test_us_roles_need_to_be_remote_and_that_is_the_only_place_rule():
    """Remote is a hard US requirement; WHERE in the US is not. A remote role anchored to
    another state is fine, an onsite role is not."""
    for loc in ["Remote (US)", "Remote - United States", "US Remote", "Remote, Michigan",
                "Grand Rapids, MI (remote)", "Work from home, Texas",
                "Home-based, Colorado"]:
        assert scan.market_of("", loc) == "US-Remote", loc
    assert scan.market_of("us", "Remote") == "US-Remote"
    for loc in ["Austin, TX", "Grand Rapids, MI", "New York, NY",
                "San Francisco, California"]:
        assert scan.market_of("", loc) is None, loc
    assert scan.market_of("us", "") is None


def test_north_american_place_names_do_not_collide_with_european_ones():
    """Three collisions that all resolve the wrong way if North America is checked after
    the European city anchors instead of before:

      "London, ON"    is Canadian, and UK_LONDON would claim it.
      "Ontario, CA"   is California, so a bare "CA" must never mean Canada.
      "Amsterdam, NY" is not the Netherlands.
    """
    assert scan.market_of("", "London, ON") == "CA"
    assert scan.market_of("", "London, Ontario") == "CA"
    # California and Washington State, both onsite, so both out -- and emphatically not
    # Canada on the strength of "Ontario" or "Vancouver".
    assert scan.market_of("", "Ontario, CA") is None
    assert scan.market_of("", "Vancouver, WA") is None
    assert scan.market_of("", "Amsterdam, NY") is None
    # ...while the European originals are untouched
    assert scan.market_of("", "London") == "UK-London"
    assert scan.market_of("", "Amsterdam") == "NL"


def test_a_remote_role_scoped_to_another_region_is_not_a_us_remote_role():
    """A req carrying a US country code but an EMEA-wide remote scope is the remote-EMEA
    posting profile.md rejects, not a US role."""
    assert scan.market_of("us", "Remote - EMEA") is None
    assert scan.market_of("us", "Remote, Europe") is None
    assert scan.market_of("", "Remote - Global") is None


def test_market_tier_orders_the_six_markets_and_fails_safe():
    assert scan.market_tier("NL") == 1
    assert scan.market_tier("IE") == scan.market_tier("UK-London") == 2
    assert scan.market_tier("BE") == scan.market_tier("CA") == 3
    assert scan.market_tier("US-Remote") == 4
    # a market the table forgot sorts last, never first
    assert scan.market_tier("XX") == scan.market_tier("") == scan.TIER_UNKNOWN
    assert scan.TIER_UNKNOWN > max(scan.MARKET_TIER.values())
    # every market market_of() can return has a tier
    for m in ["NL", "BE", "UK-London", "IE", "CA", "US-Remote"]:
        assert m in scan.MARKET_TIER, m


def test_us_titles_go_through_the_two_narrow_gates_not_the_broad_one():
    """The US is a runway backstop, so it is only worth attention for the pivot proper or
    for senior customer success, which Tom now wants because his US searches confirm remote
    and the salary band. Renewals, generic business operations and marketing ops are all
    legitimate European targets and all still out in the US."""
    for title in ["Revenue Operations Manager", "Senior RevOps Analyst",
                  "GTM Strategy Manager", "Manager, Sales Strategy & Operations",
                  "Sales Compensation Manager",
                  # these two were in the search but missing from the regex
                  "Sales Enablement Manager", "Commercial Operations Lead",
                  # senior CS, admitted now
                  "Senior Customer Success Manager", "Principal Customer Success Manager",
                  "Customer Success Manager, Enterprise", "Manager, Customer Success"]:
        assert scan.prefilter(title, "Remote (US)") is None, title
    for title in ["Renewals Manager", "Business Operations Manager",
                  "Head of Marketing Operations", "GTM Recruiter, AMER"]:
        reason = scan.prefilter(title, "Remote (US)")
        assert reason and "narrow gates" in reason, title
    # the same titles are still fine in Europe and Canada, which keep the broad gate
    for title in ["Renewals Manager", "Business Operations Manager"]:
        assert scan.prefilter(title, "Toronto, ON") is None, title
        assert scan.prefilter(title, "London") is None, title


def test_senior_cs_is_a_subset_of_the_broad_title_gate():
    """SENIOR_CS names the same clauses INCLUDE_TITLE already carries. Kept separate so the
    US gate can admit senior CS without admitting the plain title; this is the guard that
    the two do not diverge."""
    for title in ["Senior Customer Success Manager", "Principal Customer Success Manager",
                  "Lead Customer Success Manager", "Enterprise Customer Success Manager",
                  "Strategic Customer Success Manager",
                  "Customer Success Manager, Enterprise",
                  "Customer Success Lead - Carriers", "Manager, Customer Success",
                  "Head of Customer Success"]:
        assert scan.SENIOR_CS.search(title), title
        assert scan.INCLUDE_TITLE.search(title), title
    # the qualifier is the whole point: a bare title must not match
    assert not scan.SENIOR_CS.search("Customer Success Manager")
    assert not scan.SENIOR_CS.search("Customer Success Associate")


def test_a_core_revops_us_role_that_is_not_remote_is_dropped_on_location():
    reason = scan.prefilter("Revenue Operations Manager", "Austin, TX")
    assert reason and reason.startswith("location:")


def test_plain_csm_is_still_netherlands_only():
    """Widening the map must not quietly widen the CSM_ANY exception."""
    assert scan.prefilter("Customer Success Manager", "Amsterdam") is None
    for loc in ["Toronto, ON", "London", "Cork, Ireland", "Brussels", "Remote (US)"]:
        assert scan.prefilter("Customer Success Manager", loc) is not None, loc


# ---- salary floors


def _obs(stated, low, cur, high=None):
    """A scorer observation. `high` defaults to `low`, which is both a single stated figure
    and what a row scored before salary_max_base existed looks like."""
    return {"salary_stated": stated, "salary_min_base": low,
            "salary_max_base": low if high is None else high,
            "salary_currency": cur, "language_hard_requirement": False}


def test_us_comp_floor_drops_a_role_only_on_a_figure_stated_in_the_ad():
    """The US floor is Tom's own, not a legal one, and it fires on the same terms as the
    visa floors: a real, market-matched, stated figure."""
    stage, reason = scan.deep_score_disqualifier(
        {"market": "US-Remote"}, _obs(True, 110000, "USD"))
    assert stage == "below-comp-floor" and "130000 USD" in reason
    for o in [_obs(True, 130000, "USD"), _obs(True, 145000, "USD")]:
        assert scan.deep_score_disqualifier({"market": "US-Remote"}, o) == (None, None)
    # A US row that states nothing is unconfirmed pay, and that is a drop rather than a
    # survivor now: us_comp_unstated() lets a row through on a figure seen anywhere in the
    # ad, so this is where a figure that turned out not to be a salary gets settled.
    stage, _ = scan.deep_score_disqualifier({"market": "US-Remote"}, _obs(False, 0, ""))
    assert stage == "us-comp-unstated", stage
    # ...unless the FEED carried a salary, which never relied on the ad in the first place.
    assert scan.deep_score_disqualifier(
        {"market": "US-Remote", "salary": "$150,000"}, _obs(False, 0, "")) == (None, None)
    # Europe is untouched: an unstated salary there is normal, not a disqualifier.
    assert scan.deep_score_disqualifier({"market": "NL"}, _obs(False, 0, "")) == (None, None)


def test_a_band_is_only_disqualifying_when_all_of_it_is_below_the_floor():
    """The bug Tom caught. A posting is not an offer: $120k-$150k against a $130k floor was
    dropped on the 120, throwing away a role where most of the band clears and the whole
    negotiation happens inside it."""
    straddles = _obs(True, 120000, "USD", 150000)
    assert scan.deep_score_disqualifier({"market": "US-Remote"}, straddles) == (None, None)
    assert scan.salary_floor_flag("US-Remote", straddles) == ""
    # Entirely below the floor is still a drop -- no amount of negotiating reaches it.
    stage, reason = scan.deep_score_disqualifier(
        {"market": "US-Remote"}, _obs(True, 90000, "USD", 110000))
    assert stage == "below-comp-floor", stage
    assert "90000-110000" in reason and "entirely below" in reason, reason
    # A single stated figure behaves exactly as it did before.
    assert scan.salary_floor_flag("US-Remote", _obs(True, 110000, "USD")) != ""
    assert scan.salary_floor_flag("US-Remote", _obs(True, 140000, "USD")) == ""


def test_a_straddling_band_is_kept_but_flagged():
    """Kept is not the same as silent. The bottom being under the floor is a real risk and
    Tom is the one who decides on it, so it has to be on the card."""
    flags = scan.score_flags({"market": "US-Remote", "title": "Revenue Operations Manager"},
                             _obs(True, 120000, "USD", 150000))
    assert any("starts below your" in f and "130000 USD" in f for f in flags), flags
    # A band clearing the floor outright says nothing.
    clear = scan.score_flags({"market": "US-Remote", "title": "Revenue Operations Manager"},
                             _obs(True, 135000, "USD", 160000))
    assert not any("starts below" in f for f in clear), clear


def test_a_row_scored_before_the_band_existed_keeps_its_old_answer():
    """salary_max_base is new. A stored row has only a bottom, and reading a missing top as
    zero would disqualify the whole historical corpus in one pass."""
    old_row = {"salary_stated": True, "salary_min_base": 110000, "salary_currency": "USD",
               "language_hard_requirement": False}
    assert scan.salary_band(old_row) == (110000.0, 110000.0)
    assert scan.deep_score_disqualifier({"market": "US-Remote"}, old_row)[0] == "below-comp-floor"


def test_the_ad_is_read_for_pay_before_a_us_row_is_dropped():
    """A US ad that states its band was being dropped unread because LinkedIn has no salary
    column and Adzuna's guess is discarded upstream. The feed's silence is not the ad's."""
    ad = "The base salary range for this role is $120,000 - $150,000 USD plus equity."
    assert scan.us_comp_unstated({"market": "US-Remote", "description": ad}) == ""
    assert scan.us_comp_unstated({"market": "US-Remote", "description": "No pay here."})
    # A feed salary still short-circuits it, and non-US markets are never asked.
    assert scan.us_comp_unstated({"market": "US-Remote", "salary": "$140k", "description": ""}) == ""
    assert scan.us_comp_unstated({"market": "NL", "description": "No pay here."}) == ""


def test_pay_detection_ignores_money_that_is_not_a_salary():
    """The window is the whole guard. An ARR number, an hourly rate and a stipend are all
    dollar figures, and none of them is a salary."""
    assert scan.jd_pay_figures("We passed $5,000,000 in ARR and offer $50/hour.") == []
    assert scan.jd_pay_figures("A $2,500 learning stipend and a 401(k).") == []
    assert scan.jd_pay_figures("Base: $130,000 to $165,000.") == [130000.0, 165000.0]
    assert scan.jd_pay_figures("Range $120K-$150k.") == [120000.0, 150000.0]
    assert scan.jd_pay_figures("") == []


def test_no_salary_floor_guesses_across_currencies_or_from_an_estimate():
    """A USD floor is never compared to a EUR figure, and Adzuna's predicted salaries are
    discarded upstream by adzuna_salary() so they can never reach the floor check at all."""
    assert scan.salary_floor_flag("US-Remote", _obs(True, 110000, "EUR")) == ""
    assert scan.salary_floor_flag("NL", _obs(True, 50000, "USD")) == ""
    # the upstream guard: a predicted figure produces no salary string to begin with
    predicted = {"salary_min": 40000, "salary_max": 40000, "salary_is_predicted": "1"}
    assert scan.adzuna_salary(predicted, "us") == ""


def test_canada_has_no_salary_floor_because_tom_is_a_citizen():
    for o in [_obs(True, 60000, "CAD"), _obs(True, 40000, "CAD")]:
        assert scan.deep_score_disqualifier({"market": "CA"}, o) == (None, None)
    assert scan.floor_for("CA") == (0, "", "")


def test_floor_for_reports_which_kind_of_floor_a_market_has():
    assert scan.floor_for("NL") == (71304, "EUR", "visa")
    assert scan.floor_for("IE") == (68911, "EUR", "visa")
    assert scan.floor_for("US-Remote") == (130000, "USD", "comp")
    assert scan.floor_for("") == (0, "", "")
    # the two tables must not both claim a market, or the drop would be mislabelled
    assert not (set(scan.VISA_FLOORS) & set(scan.COMP_FLOORS))


def test_every_drop_stage_the_floor_check_emits_has_a_retention_budget():
    """A stage missing from DROP_KEEP_PER_STAGE silently falls back to the default, which
    is how a new disqualifier stage becomes invisible in the committed drop log."""
    for stage in ["below-visa-floor", "below-comp-floor"]:
        assert stage in scan.DROP_KEEP_PER_STAGE, stage


def test_csm_note_covers_canada_too():
    plain = dict(NO_OBS, company_standout=False)
    flags = scan.score_flags(flag_job(title="Senior Customer Success Manager",
                                      market="CA"), plain)
    assert any("CA" in f and "non-standout" in f for f in flags)


def test_both_prompts_and_the_profile_agree_about_which_markets_exist():
    """MARKETS_SENTENCE feeds both prompts and profile.md is inlined verbatim into the deep
    scorer's system prompt. A market that exists in code but in neither piece of prose is a
    market the models will quietly reject, which is how non-Dublin Ireland was being killed
    at stage one while the location gate said it was fine."""
    markets = scan.MARKETS_SENTENCE.lower()
    for word in ["netherlands", "belgium", "london", "ireland", "canada", "us"]:
        assert word in markets, word
    profile = scan.load_profile().lower()
    for word in ["netherlands", "belgium", "united kingdom", "ireland", "canada",
                 "united states"]:
        assert word in profile, word


def test_the_location_dimension_states_a_band_for_every_market():
    """The Location & Visa dimension is a preference ordering, so a market with no band
    named in the guidance gets whatever the model feels like. Guard the six."""
    guide = dict((k, g) for k, _l, _w, g in scan.RUBRIC)["location_visa"].lower()
    for word in ["netherlands", "ireland", "uk-london", "belgium", "canada", "us"]:
        assert word in guide, word
    # and it has to say, in terms, that it is not a feasibility score -- the US is the
    # market where those two readings diverge and the expensive one to get wrong
    assert "preference" in guide


def test_the_us_transfer_case_is_reachable_from_the_prompt():
    """The rubric promises a "Transfer" field for the US 4-5 band. If job_message() stops
    emitting it under that name, the band becomes unreachable and every US role floors at
    2-3 regardless of the employer's footprint."""
    guide = dict((k, g) for k, _l, _w, g in scan.RUBRIC)["location_visa"]
    assert "Transfer" in guide
    msg = scan.job_message({"title": "RevOps Manager", "company": "Acme",
                            "location": "Remote (US)", "market": "US-Remote",
                            "transfer_markets": "NL, IE", "description": "x" * 40})
    assert "Transfer: this employer also posts roles in NL, IE" in msg
    # and it stays out of the way when there is nothing to say
    assert "Transfer:" not in scan.job_message(
        {"title": "RevOps Manager", "company": "Acme", "location": "Amsterdam",
         "market": "NL", "description": "x" * 40})


# ---- the scoring queue


def _qjob(jid, market, title, **kw):
    job = {"id": jid, "market": market, "title": title, "company": "Acme",
           "description": "x" * 1000, "posted_at": "2026-09-13T00:00:00Z"}
    job.update(kw)
    return job


def _order(jobs, deferrals=None):
    return [j["id"] for j in sorted(jobs, key=lambda j: scan.priority(j, deferrals or {}),
                                    reverse=True)]


def test_the_queue_spends_the_budget_in_market_order():
    """At equal title quality the deep-scoring slots go down Tom's ordering. Before this
    existed the loop walked new_jobs in fetcher order and broke at the cap, so Adzuna --
    which runs first -- would have won every run once the US made the cap bind."""
    jobs = [_qjob(m.lower(), m, "Revenue Operations Manager")
            for m in ["US-Remote", "NL", "UK-London", "CA", "IE", "BE"]]
    order = _order(jobs)
    assert order[0] == "nl"
    assert set(order[1:3]) == {"ie", "uk-london"}
    assert set(order[3:5]) == {"be", "ca"}
    assert order[5] == "us-remote"


def test_a_repeatedly_deferred_role_escalates_past_fresh_arrivals():
    """The starvation guard, and the whole reason the queue cannot lose a role. A job past
    the cap is not marked seen, so it returns next run; the risk is that it waits until
    MAX_POST_AGE_DAYS expires it. After DEFER_ESCALATES_AFTER runs it outranks every fresh
    row whatever its market, which turns "maybe never" into "within a few runs"."""
    us = _qjob("us", "US-Remote", "Revenue Operations Manager")
    nl = _qjob("nl", "NL", "Revenue Operations Manager")
    assert _order([us, nl], {"us": scan.DEFER_ESCALATES_AFTER}) == ["us", "nl"]
    # one run short of the threshold it must NOT jump, or the guard would invert the
    # ordering on the first deferral and the market tiers would mean nothing
    assert _order([us, nl], {"us": scan.DEFER_ESCALATES_AFTER - 1}) == ["nl", "us"]
    assert scan.DEFER_ESCALATES_AFTER < scan.DEFER_CAP <= scan.MAX_POST_AGE_DAYS * 4


def test_waiting_longer_than_the_cap_does_not_keep_climbing():
    """Deferrals above DEFER_CAP tie, so the longest-waiting row cannot starve the
    second-longest in turn."""
    a = _qjob("a", "US-Remote", "Revenue Operations Manager")
    assert scan.priority(a, {"a": 50}) == scan.priority(a, {"a": scan.DEFER_CAP})


def test_the_queue_prefers_core_revops_and_a_readable_posting():
    assert _order([_qjob("csm", "NL", "Senior Customer Success Manager"),
                   _qjob("core", "NL", "Revenue Operations Manager")]) == ["core", "csm"]
    # a stub description scores badly for reasons that are not the role's fault, so it
    # should not consume a slot ahead of a posting the scorer can actually read
    assert _order([_qjob("stub", "NL", "Revenue Operations Manager", description="short"),
                   _qjob("real", "NL", "Revenue Operations Manager")]) == ["real", "stub"]


def test_the_queue_rewards_a_confirmed_sponsor_and_a_us_transfer_path():
    assert _order([_qjob("plain", "NL", "Revenue Operations Manager"),
                   _qjob("spons", "NL", "Revenue Operations Manager",
                         sponsor="sponsor")]) == ["spons", "plain"]
    assert _order([_qjob("plain", "US-Remote", "Revenue Operations Manager"),
                   _qjob("xfer", "US-Remote", "Revenue Operations Manager",
                         transfer_markets="NL, IE")]) == ["xfer", "plain"]


def test_priority_is_total_and_never_raises_on_a_sparse_row():
    """priority() runs over rows straight out of a fetcher, so every field has to be
    optional. A KeyError here takes down the whole run after the fetches have been paid
    for."""
    for row in [{}, {"id": "x"}, {"id": "x", "market": None, "title": None},
                {"id": "x", "market": "NL", "posted_at": "not a date"},
                {"id": "x", "market": "XX", "title": "Revenue Operations Manager"}]:
        key = scan.priority(row, {})
        assert isinstance(key, tuple)
        # keys must be mutually comparable, or sorted() dies on a mixed batch
        assert key < scan.priority(_qjob("best", "NL", "Revenue Operations Manager",
                                         sponsor="sponsor"), {})


def test_deferral_counters_are_pruned_to_what_is_still_waiting(tmp=None):
    """Unbounded growth is the failure mode here: without pruning this file accumulates an
    entry for every role the radar has ever passed over. seen.json is already the record
    that a job is finished with, so a counter for one is dead weight."""
    import tempfile, os as _os
    path = _os.path.join(tempfile.mkdtemp(), "deferred.json")
    kept = scan.save_deferrals({"still": 2, "scored": 1, "zero": 0}, {"still", "zero"},
                               path)
    assert kept == {"still": 2}
    assert scan.load_deferrals(path) == {"still": 2}
    # and it clamps on the way out, so a counter cannot grow past the cap on disk either
    assert scan.save_deferrals({"old": 99}, {"old"}, path) == {"old": scan.DEFER_CAP}


def test_load_deferrals_survives_a_corrupt_or_missing_file():
    """A lost counter costs one run of ordering fairness. Failing the scan over it would
    cost the whole run, after the fetches have been paid for."""
    import tempfile, os as _os
    d = tempfile.mkdtemp()
    assert scan.load_deferrals(_os.path.join(d, "nope.json")) == {}
    bad = _os.path.join(d, "bad.json")
    open(bad, "w").write('["not", "a", "dict"]')
    assert scan.load_deferrals(bad) == {}
    mixed = _os.path.join(d, "mixed.json")
    open(mixed, "w").write('{"a": 2, "b": "not a number", "c": null}')
    assert scan.load_deferrals(mixed) == {"a": 2}


# ---- hiring.cafe searches


def test_every_search_rebuilds_the_url_toms_browser_produced():
    """The searches are structured dicts, because editing percent-encoded JSON is the
    reason adding a market here was avoided for so long. The fixture is a capture of the
    five URLs Tom pasted from his own hiring.cafe session, so this asserts the code asks
    for precisely what he asked for.

    It is the guard that matters most in this file: a silent change in targeting shows up
    as a source quietly going thin, not as an error."""
    import json as _json
    import os as _os
    import urllib.parse as _url
    fixture = _os.path.join(_os.path.dirname(__file__), "fixtures",
                            "hiringcafe-searchstate.json")
    with open(fixture, encoding="utf-8") as f:
        want = _json.load(f)
    assert sorted(want) == sorted(scan.APIFY_HIRINGCAFE_SEARCHES)
    for label, expect in want.items():
        url = scan.hiringcafe_url(scan.APIFY_HIRINGCAFE_SEARCHES[label])
        got = _json.loads(_url.parse_qs(_url.urlparse(url).query)["searchState"][0])
        assert got == expect, label


def test_the_search_titles_and_the_us_code_gate_cannot_drift_apart():
    """Every phrase the RevOps search asks for has to be one REVOPS_CORE keeps. A term in
    the search that the regex misses is a row Apify is paid for and prefilter() then throws
    away -- which is exactly what was happening to "sales enablement" and "commercial
    operations"."""
    import re as _re
    phrases = _re.findall(r'"([^"]+)"', scan.HC_REVOPS_TITLES)
    assert len(phrases) == 18
    for phrase in phrases:
        assert scan.REVOPS_CORE.search(phrase), phrase
        assert scan.INCLUDE_TITLE.search(phrase), phrase


def test_no_whole_country_us_or_uk_location_exists_anywhere():
    """Two location shapes that would both pay for rows the location gate then drops: a
    whole-UK search (market_of accepts London and the commuter belt only) and a whole-US
    one (the US searches are Grand Rapids plus flexible_regions instead)."""
    for label, state in scan.APIFY_HIRINGCAFE_SEARCHES.items():
        for loc in state["locations"]:
            ac = loc["address_components"][0]
            assert not (ac["short_name"] == "GB" and loc["types"] == ["country"]), label
            assert not (ac["short_name"] == "US" and loc["types"] == ["country"]), label


def test_the_european_searches_never_ask_for_remote():
    """profile.md rejects remote-from-anywhere and remote-EMEA, and workplace_types is how
    that is enforced at source rather than paid for and dropped. Canada is exempt: Tom is a
    citizen and can work anywhere in it."""
    for label in ("revops", "cs-eu-ca", "cs-nl"):
        for loc in scan.APIFY_HIRINGCAFE_SEARCHES[label]["locations"]:
            short = loc["address_components"][0]["short_name"]
            if short == "CA":
                continue
            assert "Remote" not in loc["workplace_types"], f"{label}/{short}"


def test_both_us_searches_require_remote_and_a_transparent_salary():
    """The US rows are only wanted because the search confirms remote and the salary band.
    The code leans on that: every US row must carry a stated salary."""
    for label in ("us-revops", "us-cs"):
        state = scan.APIFY_HIRINGCAFE_SEARCHES[label]
        assert state["restrictJobsToTransparentSalaries"] is True, label
        assert state["locations"][0]["workplace_types"] == ["Remote"], label
        # home first, then outward -- "ideally Michigan but anywhere remote"
        flexible = state["locations"][0]["options"]["flexible_regions"]
        assert "anywhere_in_country" in flexible, label


def test_every_hiringcafe_search_produces_a_usable_url():
    for label, state in scan.APIFY_HIRINGCAFE_SEARCHES.items():
        url = scan.hiringcafe_url(state)
        assert url.startswith("https://hiringcafe.com/?searchState="), label
        assert " " not in url, label


def test_gtm_alone_is_not_enough_for_the_us_gate():
    """A bare \\bgtm\\b admitted "GTM Recruiter, AMER" and "Staff, Analytics Engineer, GTM
    Data Science" on the first US run -- a recruiting role and an engineering role. Both
    would have died at the Haiku screen, but the point of this gate is that US volume dies
    for free, before anything is spent on it. GTM now needs an ops/strategy noun beside
    it, in either word order."""
    for title in ["GTM Recruiter, AMER (Fixed Term)",
                  "Staff, Analytics Engineer, GTM Data Science",
                  "GTM Data Scientist", "Program Manager, GTM Strategic Programs"]:
        assert not scan.REVOPS_CORE.search(title), title
    for title in ["GTM Operations Process Architect", "GTM Strategy Manager",
                  "Go-to-Market Operations Lead", "Operations, GTM",
                  "GTM Systems Manager", "GTM Enablement Lead"]:
        assert scan.REVOPS_CORE.search(title), title


def test_marketing_ops_is_off_target_and_so_is_out_of_the_us_gate():
    """profile.md lists Marketing Ops as off-target for Domain. It stays in INCLUDE_TITLE
    for Europe, where the deep scorer weighs it, but the US gate is the pivot proper."""
    assert not scan.REVOPS_CORE.search("Head of Marketing Operations")
    assert scan.INCLUDE_TITLE.search("Head of Marketing Operations")
    assert scan.prefilter("Head of Marketing Operations", "Amsterdam") is None


def test_the_north_american_source_subsets_are_filters_over_the_full_lists():
    """Each North American subset is derived from the European list rather than written
    out again, so a term added to the main list cannot be silently missing from the
    narrow one."""
    assert set(scan.ADZUNA_CORE_PHRASES) <= set(scan.ADZUNA_PHRASES)
    assert set(scan.JOBSPY_CORE_TERMS) <= set(scan.JOBSPY_TERMS)
    assert scan.ADZUNA_CORE_PHRASES and scan.JOBSPY_CORE_TERMS


def test_every_adzuna_country_has_a_currency_because_the_api_reports_none():
    """Adzuna returns pay with no currency at all, so the code has to supply it, and
    salary_floor_flag() compares the stated currency against the market's before it will
    drop anything. A country with no entry here produces figures the floor check silently
    ignores."""
    for cc in ["nl", "gb", "ca", "us"]:
        assert scan.ADZUNA_COUNTRIES.get(cc), cc
    for cc in scan.ADZUNA_CORE_COUNTRIES:
        assert cc in scan.ADZUNA_COUNTRIES, cc


def test_jobspy_targets_are_whole_countries_not_cities():
    """Scoping Ireland to "Dublin, Ireland" made Indeed's own location filter do the
    Dublin-only narrowing the location gate used to do."""
    for t in scan.JOBSPY_TARGETS:
        assert "," not in t["location"], t["location"]
        assert t["cc"] in ("ie", "ca", "us")
        assert t["sites"] and t["currency"]
    # Google Jobs is what replaces hiring.cafe's long-tail reach in North America: it
    # indexes Greenhouse/Lever/Ashby posting pages directly.
    na = [t for t in scan.JOBSPY_TARGETS if t["cc"] in ("ca", "us")]
    assert na and all("google" in t["sites"] for t in na)
    # the US search asks for remote at the source, since market_of() drops the rest
    us = [t for t in scan.JOBSPY_TARGETS if t["cc"] == "us"][0]
    assert all("remote" in term for term in us["terms"])


def test_a_us_row_with_no_published_salary_is_dropped_before_any_model_call():
    """Tom only wants US roles confirmed to pay well, and both US searches set
    restrictJobsToTransparentSalaries, so this is the code agreeing with the search. It
    runs alongside says_no_sponsorship() and requires_other_language(), before either
    model call, so it costs nothing."""
    assert scan.us_comp_unstated({"market": "US-Remote", "salary": ""})
    assert scan.us_comp_unstated({"market": "US-Remote"})
    assert scan.us_comp_unstated({"market": "US-Remote", "salary": "   "})
    assert not scan.us_comp_unstated({"market": "US-Remote",
                                      "salary": "130000-160000 USD"})
    # ...and it is US-only. Europe mostly publishes no salary at all and Canada has no
    # floor, so applying this anywhere else would empty the board.
    for market in ("NL", "IE", "UK-London", "BE", "CA"):
        assert not scan.us_comp_unstated({"market": market, "salary": ""}), market


def test_the_us_salary_rule_is_presence_here_and_value_downstream():
    """Two checks, deliberately different. This one asks "is there a number at all" off the
    feed, for free. COMP_FLOORS asks "does the number clear the floor" off the JD, which is
    the only salary source worth gating a value on."""
    # present but below the floor: survives the free check, dropped after scoring
    row = {"market": "US-Remote", "salary": "90000-110000 USD"}
    assert not scan.us_comp_unstated(row)
    stage, _why = scan.deep_score_disqualifier(
        row, {"salary_stated": True, "salary_min_base": 90000,
              "salary_currency": "USD", "language_hard_requirement": False})
    assert stage == "below-comp-floor"


def test_the_new_drop_stage_is_visible_in_the_log_and_on_the_dashboard():
    """An unregistered stage falls back to the default retention and goes half-invisible in
    the committed drop log, and an unlabelled one renders as a raw slug."""
    assert "us-comp-unstated" in scan.DROP_KEEP_PER_STAGE
    page = open(os.path.join(os.path.dirname(__file__), "..", "docs", "index.html"),
                encoding="utf-8").read()
    assert '"us-comp-unstated"' in page


# ---- the revopsroles digest


def test_the_digest_sender_filter_matches_the_domain_not_one_mailbox():
    """Tom runs two alerts now, Europe and the US, and they arrive as separate emails. IMAP
    SEARCH FROM is a substring match on the header, so filtering on the domain means a new
    alert or a changed sender cannot silently drop a feed -- and a silently missing feed
    looks exactly like a quiet day on the status line."""
    assert scan.REVOPSROLES_SENDER == "revopsroles.com"
    assert "@" not in scan.REVOPSROLES_SENDER


def test_the_digest_work_mode_tag_rescues_us_rows_and_leaves_europe_alone():
    """The work mode is a tag in this digest, not part of the location, and market_of()
    only reads the location. Folding the tag in is US-only on purpose: remote is the
    REQUIREMENT there, but in Europe remote wording next to a bare country is how
    remote-EMEA reqs get rejected, so doing this everywhere would drop a genuine "Ireland"
    row that is kept today."""
    def gate(loc, mode, cc):
        return f"{loc} ({mode})" if mode and cc == "us" else loc

    # US: a city row tagged Remote is a US remote role
    assert scan.market_of("us", gate("Austin, United States", "Remote", "us")) == "US-Remote"
    assert scan.market_of("us", gate("San Francisco, CA, United States", "Remote",
                                     "us")) == "US-Remote"
    # ...and on-site or hybrid still resolves to nothing
    for mode in ("Onsite", "Hybrid"):
        assert scan.market_of("us", gate("Austin, United States", mode, "us")) is None, mode
    # Europe untouched, including the case the restriction exists for
    assert scan.market_of("ie", gate("Ireland", "Remote", "ie")) == "IE"
    assert scan.market_of("nl", gate("Amsterdam, Netherlands", "Remote", "nl")) == "NL"
    assert scan.market_of("gb", gate("London, United Kingdom", "Remote", "gb")) == "UK-London"
    # Canada needs no help: its branch accepts remote already
    assert scan.market_of("ca", gate("Toronto, Canada", "Remote", "ca")) == "CA"


def test_the_digest_resolves_country_codes_for_the_new_markets():
    """country_code() is what turns "Austin, United States" into cc="us", which is the flag
    the work-mode fix keys on. Without the entry it returned "" and the fix never fired."""
    for name, want in [("united states", "us"), ("usa", "us"), ("canada", "ca"),
                       ("united kingdom", "gb"), ("ireland", "ie"),
                       ("the netherlands", "nl"), ("belgium", "be")]:
        assert scan.country_code(name) == want, name
    # every market the radar has must be reachable from a country name in a digest
    assert {scan.country_code(n) for n in ("united states", "canada", "ireland",
                                           "united kingdom", "the netherlands",
                                           "belgium")} == {"us", "ca", "ie", "gb", "nl", "be"}


def _digest_html():
    with open(os.path.join(os.path.dirname(__file__), "fixtures",
                           "revopsroles-digest.html"), encoding="utf-8") as f:
        return f.read()


def test_the_digest_parser_reads_every_field_off_a_real_email():
    """Pinned against a real digest, because the failure mode for this source is not an
    exception -- it is a selector going stale after a restyle and a field quietly emptying
    for months."""
    rows = scan.parse_revopsroles_jobs(_digest_html())
    assert len(rows) == 2
    first = rows[0]
    assert first["id"] == "b6fe6ccd-bd3e-43ad-bb68-2b89a868dfec"
    assert first["title"] == ("Associate Director, Sales Compensation Data Architecture "
                              "and Strategy")
    assert first["company"] == "S&P Global"        # &amp; unescaped
    assert first["location"] == "London, United Kingdom"
    assert first["salary"] == "$104k – $183k"
    assert (first["category"], first["seniority"]) == ("Sales Ops", "Director")
    assert rows[1]["salary"] == "$86k – $153k"
    assert rows[1]["seniority"] == "Manager"
    # Every row must carry a salary, which is the whole regression: the old selector was
    # color:#16a34a and found nothing after the site restyled.
    assert all(r["salary"] for r in rows)


def test_the_digest_salary_survives_a_restyle():
    """The selector is the SHAPE of the text, not the colour of the span around it. A
    colour is presentation and already changed once -- #16a34a became #9e4d00 and nothing
    failed loudly, it just emptied the field on 32 rows. This asserts the next restyle
    cannot do the same."""
    html = _digest_html()
    assert "#16a34a" not in html          # the colour the old selector wanted is gone
    assert "color:#9e4d00" in html
    restyled = html.replace("#9e4d00", "#abcdef")
    rows = scan.parse_revopsroles_jobs(restyled)
    assert [r["salary"] for r in rows] == ["$104k – $183k", "$86k – $153k"]


def test_the_digest_never_mistakes_a_location_for_a_salary():
    """The company and location live in a span too, and a US location can carry digits
    ("Palo Alto, CA 94301"). Reading the first money-shaped span without skipping those
    would put a zip code in the salary field."""
    html = _digest_html().replace("London, United Kingdom", "Palo Alto, CA 94301")
    rows = scan.parse_revopsroles_jobs(html)
    assert rows[0]["location"] == "Palo Alto, CA 94301"
    assert rows[0]["salary"] == "$104k – $183k"


def test_the_digest_tolerates_a_block_with_fewer_tags():
    """The email Tom forwarded carries only two tags, category and seniority, with no work
    mode at all -- so indexing tags[2] blind would throw on a real digest."""
    rows = scan.parse_revopsroles_jobs(_digest_html())
    assert all(r["work_mode"] == "" for r in rows)
    # and a block with no tag div at all still parses
    stripped = _digest_html().replace('margin-top:8px', 'margin-top:9px')
    rows = scan.parse_revopsroles_jobs(stripped)
    assert len(rows) == 2
    assert all(r["category"] == "" and r["seniority"] == "" for r in rows)
    assert all(r["salary"] for r in rows)       # salary is independent of the tags


def test_the_digest_parser_is_total_on_junk():
    """It runs over whatever arrives in an inbox, so it has to return rather than raise."""
    for body in ["", None, "<html>no jobs here</html>",
                 '<a href="https://revopsroles.com/jobs/">malformed</a>']:
        assert scan.parse_revopsroles_jobs(body) == []


def test_a_digest_row_with_no_salary_still_cannot_pass_the_us_gate():
    """The parser reads salary correctly now, but a posting that genuinely states none must
    still drop for the US -- that rule is about the role's pay being confirmed, not about
    the parser working."""
    row = {"market": "US-Remote", "salary": "", "source": "revopsroles"}
    assert scan.us_comp_unstated(row)
    assert not scan.us_comp_unstated(dict(row, salary="$150k – $180k"))
    # the same row for Europe is unaffected -- most EU postings state no salary at all
    assert not scan.us_comp_unstated(dict(row, market="IE"))


# ---- thin evidence


def test_a_stub_is_not_presented_to_the_scorer_as_a_description():
    """The measured problem this fixes. revopsroles rows carry a ~50-character synthesized
    summary and job_message() used to emit it under "Description:" as though it were the
    posting, so the model had no way to know it was guessing. It showed in the numbers:
    stub rows scored a 6.03 mean against 5.78 for rows with a real JD, and 22 of 59 cleared
    the gate. Absence of evidence was reading as absence of problems."""
    stub = "Category: CS Ops; Seniority: Senior; Work mode: Hybrid"
    msg = scan.job_message({"title": "Senior Customer Success Manager", "company": "Acme",
                            "location": "Amsterdam", "market": "NL", "description": stub})
    assert "EVIDENCE: THIN" in msg
    assert "Description:" not in msg          # never dressed up as one
    assert stub in msg                        # but what IS known is still passed on
    # and it says the disqualifiers did not run, so a silent posting is not a clean one
    assert "could not run" in msg

    # a real posting is presented normally
    full = scan.job_message({"title": "RevOps Manager", "company": "Acme",
                             "location": "Amsterdam", "market": "NL",
                             "description": "x" * (scan.MIN_DESC_CHARS + 1)})
    assert "EVIDENCE: THIN" not in full and "Description:" in full


def test_thin_is_the_same_threshold_the_fetcher_retries_on():
    """One definition, or the scorer's warning and the fetcher's retry disagree about what
    counts as a posting."""
    assert scan.is_thin("") and scan.is_thin(None)
    assert scan.is_thin("x" * (scan.MIN_DESC_CHARS - 1))
    assert not scan.is_thin("x" * scan.MIN_DESC_CHARS)
    assert scan.is_thin("   " + "x" * 10 + "   ")       # whitespace is not evidence


def test_a_thin_row_is_flagged_on_the_card_not_just_in_the_prompt():
    """A score built on a title and a location is a different kind of number from one built
    on a posting, and the difference has to be visible where Tom reads it."""
    thin = [f for f in scan.score_flags(
        flag_job(description="Category: CS Ops"), NO_OBS) if "thin evidence" in f]
    assert thin and "could not run" in thin[0]
    assert not [f for f in scan.score_flags(flag_job(), NO_OBS) if "thin evidence" in f]


def test_the_scorer_is_told_how_to_handle_thin_evidence():
    """The prompt has to say what to DO about it, not just that it happened -- otherwise
    the model fills the gap with what a role of that title usually involves, which is the
    exact failure the posting exists to prevent."""
    system = scan.score_system()
    assert "EVIDENCE: THIN" in system
    assert "Thin evidence" in system
    # and it must name the disqualifier consequence, which is the dangerous half
    assert "unrun" in system or "not treat a silent posting as a clean one" in system


def test_the_thin_stage_label_exists_on_the_dashboard():
    page = open(os.path.join(os.path.dirname(__file__), "..", "docs", "index.html"),
                encoding="utf-8").read()
    assert '"thin-evidence"' in page


# ---- rescuing a stub description from the company's own board


def _board(*entries):
    """A fake fetch for findform.find_board: a Greenhouse board with these roles."""
    import json as _json

    class R:
        status_code = 200

        def json(self):
            return {"jobs": [{"id": i, "title": t, "absolute_url": f"https://x/{i}",
                              "location": {"name": loc}}
                             for i, (t, loc) in enumerate(entries, 1)]}

    return lambda url, **kw: R()


def test_the_rescue_is_stricter_about_location_than_an_application_is():
    """rank() accepts a single close title match even when nothing sits in the posting's
    market, so a board that omits locations does not rule out the whole company. That is
    the right trade-off for finding a form, which Tom confirms by eye.

    It is the wrong one for a description. A board saying "Austin, TX" against an Amsterdam
    row is not missing a location, it is stating a different one, and attaching that JD
    hands the scorer the wrong comp and requirements -- a confidently wrong score, which is
    what the thin-evidence work exists to prevent."""
    import findform
    # rank() on its own WOULD take it: one close title, no market match, so it is returned
    jobs = findform.board_jobs("greenhouse", "acme",
                               _board(("Revenue Operations Manager", "Austin, TX")))
    _m, best = findform.rank(jobs, "Revenue Operations Manager", "NL")
    assert best is not None and not findform.same_market("NL", best["location"])
    # ...and the rescue refuses it anyway
    assert scan.rescue_description(
        {"company": "Acme", "title": "Revenue Operations Manager", "market": "NL"}, [], {},
        _board(("Revenue Operations Manager", "Austin, TX"))) == ("", "")
    # a stated location IN the market is fine
    desc, url = scan.rescue_description(
        {"company": "Acme", "title": "Revenue Operations Manager", "market": "NL"}, [], {},
        _board(("Revenue Operations Manager", "Amsterdam, Netherlands")))
    assert url                       # matched; the text fetch is a separate network step
    # and a title that merely reads similar is refused by the title gate
    assert findform.title_score("Revenue Operations Manager",
                                "Revenue Operations Manager, Post Sales (EMEA)") < \
        findform.TITLE_MATCH_MIN


def test_the_rescue_returns_nothing_rather_than_something_wrong():
    """Every failure path hands back ("", "") or at most a URL with no text. A row keeps
    its stub and its thin-evidence flag rather than being given a JD that is not its own."""
    assert scan.rescue_description({"company": "", "title": "X"}, [], {}) == ("", "")
    assert scan.rescue_description({"company": "Acme", "title": "Revenue Operations "
                                    "Manager", "market": "NL"}, [], {},
                                   _board()) == ("", "")
    # a board with only an unrelated role resolves nothing
    assert scan.rescue_description(
        {"company": "Acme", "title": "Revenue Operations Manager", "market": "NL"}, [], {},
        _board(("Warehouse Associate", "Amsterdam"))) == ("", "")


def test_the_rescue_runs_before_the_hard_disqualifiers_not_after():
    """The ordering IS the feature. says_no_sponsorship() and requires_other_language() both
    read the description, so on a stub neither can fire and a role that rules out
    sponsorship in its JD passes both silently. Recovering the text first is what makes
    them work on these rows, and that is worth more than the score."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "scan.py"),
               encoding="utf-8").read()
    body = src[src.index("for j in new_jobs[:MAX_SCREENED_PER_RUN]:"):]
    rescue_at = body.index("rescue_description(")
    sponsor_at = body.index("says_no_sponsorship(")
    language_at = body.index("requires_other_language(")
    assert rescue_at < sponsor_at, "rescue must run before the sponsorship check"
    assert rescue_at < language_at, "rescue must run before the language check"


def test_smartrecruiters_postings_give_up_their_jd():
    """The board lookup was resolving SmartRecruiters URLs and recovering nothing: their
    page renders client-side so the JSON-LD extractor finds no JobPosting. The API has it.
    Registered as free-if-no-match, so trying it on another host costs no request."""
    assert scan.smartrecruiters_desc("https://acme.com/careers/1") == ""
    assert scan.smartrecruiters_desc("") == ""
    assert scan.SMARTRECRUITERS_URL.match(
        "https://jobs.smartrecruiters.com/QIMA/744000147877009")
    assert scan.smartrecruiters_desc in scan.GENERIC_FETCHERS
    assert scan.smartrecruiters_desc in scan._FREE_IF_NO_MATCH


def test_every_generic_fetcher_that_can_be_free_is_marked_free():
    """A fetcher not in _FREE_IF_NO_MATCH burns one of MAX_DESC_FETCHES on a URL it was
    never going to handle."""
    for fetcher in (scan.workday_desc, scan.greenhouse_board_desc,
                    scan.smartrecruiters_desc):
        assert fetcher in scan._FREE_IF_NO_MATCH, fetcher.__name__
    # the generic JSON-LD one is deliberately NOT free: it works on any host
    assert scan.jsonld_job_description not in scan._FREE_IF_NO_MATCH


# ---- web-searching for a posting (last resort)


def test_the_search_only_accepts_a_url_and_treats_anything_else_as_none():
    """The model is asked for one line and nothing else. Every other shape of answer has to
    read as "not found" rather than as something to go fetch."""
    calls = []
    real = scan._claude_call

    def fake(reply):
        def _c(*a, **kw):
            calls.append(kw.get("tools"))
            return reply
        return _c

    try:
        for reply, want in [
                ("URL: https://jobs.ashbyhq.com/acme/1", "https://jobs.ashbyhq.com/acme/1"),
                ("URL: NONE", ""),
                ("URL: none", ""),
                ("URL: https://acme.com/x.", "https://acme.com/x"),   # trailing period
                ("I could not find it.", ""),
                ("URL: not-a-url", ""),
                ("", ""),
        ]:
            scan._claude_call = fake(reply)
            assert scan.search_jd_url("k", {"company": "Acme", "title": "X"}) == want, reply
        # a failing call costs the tokens and nothing else
        def boom(*a, **kw):
            raise RuntimeError("api down")
        scan._claude_call = boom
        assert scan.search_jd_url("k", {"company": "Acme", "title": "X"}) == ""
    finally:
        scan._claude_call = real
    # and it really does ask for web search
    assert any(t and t[0]["type"] == "web_search_20260209" for t in calls if t)
    assert all(t[0]["max_uses"] == scan.JD_SEARCH_MAX_USES for t in calls if t)


def test_the_search_result_is_verified_in_code_not_taken_on_trust():
    """The split is what makes spending tokens here safe. The model points at a page; code
    decides whether it is the same role, off the page's own schema.org metadata. So a bad
    search costs a refused match and a row that stays thin -- the outcome it already had --
    rather than a plausible score built on another role's requirements."""
    posting = {"title": "Revenue Operations Associate",
               "hiringOrganization": {"name": "Omnea"},
               "description": "<p>" + "x" * (scan.MIN_DESC_CHARS + 50) + "</p>"}
    real = scan.jsonld_job_posting
    try:
        scan.jsonld_job_posting = lambda url: posting
        job = {"company": "Omnea", "title": "Revenue Operations Associate"}
        desc, url = scan.verify_jd("https://x/1", job)
        assert desc and url == "https://x/1"
        # company suffixes are the same employer
        assert scan.verify_jd("https://x/1", dict(job, company="Omnea Ltd"))[0]
        # a different employer is refused, however well the title matches
        assert scan.verify_jd("https://x/1", dict(job, company="Stripe")) == ("", "")
        # so is a different role at the right employer
        assert scan.verify_jd("https://x/1",
                              dict(job, title="Warehouse Associate")) == ("", "")
        # a page with no JobPosting metadata at all is refused
        scan.jsonld_job_posting = lambda url: {}
        assert scan.verify_jd("https://x/1", job) == ("", "")
        # ...and so is one whose description is itself a stub
        scan.jsonld_job_posting = lambda url: dict(posting, description="too short")
        assert scan.verify_jd("https://x/1", job) == ("", "")
    finally:
        scan.jsonld_job_posting = real


def test_the_search_uses_the_same_title_gate_as_everything_else():
    """One threshold for "is this the same role", shared with the application lookup and
    the board rescue."""
    import findform
    src = open(os.path.join(os.path.dirname(__file__), "..", "scan.py"),
               encoding="utf-8").read()
    body = src[src.index("def verify_jd("):src.index("def is_thin(")]
    assert "findform.TITLE_MATCH_MIN" in body
    assert "findform.title_score" in body
    assert findform.TITLE_MATCH_MIN >= 0.85


def test_the_search_is_the_last_thing_tried_before_a_row_is_set_aside():
    """It used to sit in pass B, spent only on rows that had survived the screen and won a
    scoring slot. That cannot survive setting stubs aside: a row with no text no longer
    reaches pass B at all, so the search would never run for exactly the rows it exists
    for. It now sits at the end of pass A's recovery chain -- after the free routes, before
    the row is given up on."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "scan.py"),
               encoding="utf-8").read()
    passa = src[src.index("for j in new_jobs[:MAX_SCREENED_PER_RUN]"):
                src.index("# PASS B: deep score")]
    search_at = passa.index("search_jd_url(")
    # the free routes are exhausted first: the stored/fetched text, then the company board
    assert passa.index("fill_description(j)") < search_at
    assert passa.index("rescue_description(") < search_at
    # and it runs before the row can be set aside, or the budget would never be spent
    assert search_at < passa.index("set_aside.append(")
    # the disqualifiers read whatever it recovered, so they need no second run of their own
    assert search_at < passa.index("says_no_sponsorship(")
    assert search_at < passa.index("requires_other_language(")


def test_a_row_with_no_posting_is_set_aside_rather_than_scored_or_dropped():
    """Tom's call, and the two halves matter equally. Not scored: a number built on a title
    and a location is a different kind of object from one built on a posting, and ranking
    them together makes the list lie. Not dropped: the hard disqualifiers could not run on
    text that short, so the role is unexamined, not rejected."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "scan.py"),
               encoding="utf-8").read()
    passa = src[src.index("for j in new_jobs[:MAX_SCREENED_PER_RUN]"):
                src.index("# PASS B: deep score")]
    aside = passa.index("set_aside.append(")
    # before the screen and before any scoring, so neither model is ever paid for a stub
    assert aside < passa.index("screen_job(")
    assert "score_job(" not in passa
    # and it is not a drop: no record_drop between the sponsor check and the set-aside
    branch = passa[passa.index('if is_thin(j["description"]):'):aside]
    assert "record_drop(" not in branch, branch
    # the marker the dashboard reads, via the one shape both callers share
    assert "as_set_aside(j)" in branch
    assert scan.as_set_aside({"market": "NL", "score": 8.2})["evidence"] == "thin"


def test_set_aside_leaves_nothing_of_the_score_behind():
    """A stale score's supporting detail next to "not scored" is worse than either alone."""
    row = scan.as_set_aside({"market": "NL", "score": 8.2, "score_raw": 8.2,
                             "dimensions": {"experience": 7}, "found_at": "2026-09-01",
                             "comp": {"stated": True, "min_base": 90000},
                             "market_conflict": {"stated": "Austin, TX"},
                             "flags": ["title band: analyst"], "verdict": "Looks good."})
    assert row["score"] is None and row["score_raw"] is None
    assert row["dimensions"] == {} and row["flags"] == [scan.SET_ASIDE_FLAG]
    assert "comp" not in row and "market_conflict" not in row
    # found_at drives the 7-day age filter; refreshing it would resurrect expired postings
    assert row["found_at"] == "2026-09-01"


def test_a_scored_stub_already_on_the_dashboard_is_converted():
    """The scan stops producing these, but a scored stub is carried forward verbatim by
    every later run, so without this an 8.2 built on 54 characters sits there until it ages
    out. Only rows that are thin right now, and only ones that were actually scored."""
    now = scan.datetime.now(scan.timezone.utc)
    fresh = (now - scan.timedelta(days=1)).isoformat()
    rows = [
        {"id": "stub", "found_at": fresh, "description": "x" * 54, "score": 8.2},
        {"id": "real", "found_at": fresh, "description": "x" * 4000, "score": 7.1},
        {"id": "already-aside", "found_at": fresh, "description": "x" * 54, "score": None},
        {"id": "stale", "found_at": (now - scan.timedelta(days=30)).isoformat(),
         "description": "x" * 54, "score": 6.0},
    ]
    hit = scan.set_aside_thin(rows, 7, now=now)
    assert [j["id"] for j in hit] == ["stub"], [j["id"] for j in hit]
    assert rows[0]["score"] is None and rows[1]["score"] == 7.1
    assert rows[3]["score"] == 6.0          # outside the window, left alone


def test_the_search_budget_is_small_and_bounded():
    assert scan.MAX_JD_SEARCHES_PER_RUN <= 5
    assert scan.JD_SEARCH_MAX_USES <= 5
    assert scan.SERVER_TOOL_MAX_TURNS >= 2      # pause_turn needs at least one resume
    # a lookup, not a judgement: this must not be the expensive model
    assert scan.JD_SEARCH_MODEL != scan.CLAUDE_SCORE_MODEL


def test_a_server_tool_call_resumes_across_pause_turn():
    """The API answers pause_turn when its own tool loop needs resuming, and the assistant
    turn goes straight back with no extra user message. Without tools the loop must run
    exactly once, so no existing caller changes behaviour."""
    seen_bodies = []

    class R:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    replies = [
        {"stop_reason": "pause_turn", "content": [{"type": "server_tool_use"}], "usage": {}},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "URL: NONE"}],
         "usage": {}},
    ]
    real_post = scan.requests.post
    try:
        def post(url, **kw):
            seen_bodies.append(kw["json"])
            return R(replies[len(seen_bodies) - 1])
        scan.requests.post = post
        out = scan._claude_call("k", "m", "sys", "user", 100,
                                tools=[{"type": "web_search_20260209",
                                        "name": "web_search"}])
        assert out == "URL: NONE"
        assert len(seen_bodies) == 2
        # the resumed request carries the assistant turn and no new user message
        assert seen_bodies[1]["messages"][-1]["role"] == "assistant"
        assert len(seen_bodies[1]["messages"]) == 2
    finally:
        scan.requests.post = real_post


# ---------------------------------------------------------------- market conflicts
#
# A feed can be wrong about where a job is: hiring.cafe put a Canadian role in Dublin. The
# market is not cosmetic downstream -- it picks the CV's contact block and it decides how
# the application FORM answers the visa questions -- so the disagreement has to be caught.

def test_a_posting_that_contradicts_its_market_is_caught():
    job = flag_job(market="IE", location="Dublin, Ireland")
    conflict = scan.market_conflict(job, {**NO_OBS, "posting_location": "Toronto, ON"})
    assert conflict == {"stated": "Toronto, ON", "resolves_to": "CA"}, conflict


def test_a_posting_that_agrees_with_its_market_is_not_a_conflict():
    job = flag_job(market="IE")
    for stated in ("Dublin, Ireland", "Cork", "Ireland"):
        assert scan.market_conflict(job, {**NO_OBS, "posting_location": stated}) is None, stated


def test_a_location_outside_every_target_market_is_not_a_conflict():
    """The common case, and the one that would make this useless if it fired. Remote-EMEA
    wording and a bare country next to "remote" both resolve to no market at all, and
    calling that a disagreement would flag most rows while saying nothing."""
    job = flag_job(market="IE")
    for stated in ("Remote - EMEA", "Global", "Berlin, Germany", "Singapore", ""):
        assert scan.market_conflict(job, {**NO_OBS, "posting_location": stated}) is None, stated


def test_a_row_with_no_market_is_never_in_conflict_with_itself():
    assert scan.market_conflict(flag_job(market=""), {**NO_OBS,
                                                      "posting_location": "Toronto"}) is None


def test_the_conflict_reaches_the_row_as_data_and_as_a_flag():
    """Both, on purpose. The flag is so it is visible on the dashboard; the structured copy
    is because the apply queue has to ACT on it -- parsing it back out of a flag string
    would be the wrong kind of clever."""
    job = flag_job(market="IE")
    data = {**NO_OBS, "posting_location": "Vancouver, BC", "dimensions": dims(),
            "flags": [], "verdict": "ok"}
    result = scan.parse_score_result(job, data)
    assert result["market_conflict"] == {"stated": "Vancouver, BC", "resolves_to": "CA"}
    assert any("location conflict" in f and "CA" in f for f in result["flags"]), result["flags"]


def test_an_agreeing_row_carries_no_conflict_key_value():
    job = flag_job(market="NL")
    data = {**NO_OBS, "posting_location": "Amsterdam", "dimensions": dims(),
            "flags": [], "verdict": "ok"}
    result = scan.parse_score_result(job, data)
    assert result["market_conflict"] is None
    assert not any("location conflict" in f for f in result["flags"])


def test_the_scorer_is_asked_for_the_location_and_told_where_it_goes():
    """The prompt tells the model to TRUST the resolved market and not second-guess it,
    which is why it cannot be the thing that raises the alarm. It has to be pointed at
    posting_location instead, or the two instructions just contradict each other."""
    sys_prompt = scan.score_system()
    assert "posting_location" in sys_prompt
    assert "Trust it for SCORING" in sys_prompt
    # And the schema has to require it, or the model may simply omit it.
    assert "posting_location" in scan.SCORE_SCHEMA["required"]
    assert "posting_location" in scan.SCORE_SCHEMA["properties"]


# --- storing the ad, and recovering one stored short ------------------------------------
#
# DESC_STORE_CAP used to be 1200, sized for auditing a score. That file now feeds the
# application workflow, and a head-and-tail sample is useless for tailoring a bullet because
# what it drops is the middle. These pin the cap's new job and the one-off repair for rows
# written under the old one.

def test_a_whole_ad_survives_being_stored():
    """The cap is a sanity guard, not a sample size. The longest real posting on record is
    just under 24k characters; anything of ordinary length has to come back byte-identical."""
    ad = "Responsibilities. " * 700          # ~12.6k chars, above the old cap, below the new
    assert len(ad) > 1200, "fixture must exceed the cap this test exists to guard"
    assert scan.sample_desc(ad, scan.DESC_STORE_CAP) == ad
    assert "[...]" not in scan.sample_desc(ad, scan.DESC_STORE_CAP)


def test_the_store_cap_still_guards_a_pathological_ad():
    """Unbounded storage is the other failure. Something far past any real posting still
    gets sampled rather than committed whole."""
    monster = "x" * (scan.DESC_STORE_CAP + 5000)
    out = scan.sample_desc(monster, scan.DESC_STORE_CAP)
    assert len(out) == scan.DESC_STORE_CAP
    assert "[...]" in out


def test_backfill_targets_only_rows_that_were_cut_short():
    """A row is a target when its stored text is shorter than the desc_chars recorded before
    truncation. A row that was always short -- a stub, a dead link -- has the two equal and
    there is no fuller version to go and get, so re-requesting it is wasted."""
    now = scan.datetime.now(scan.timezone.utc)
    fresh = (now - scan.timedelta(days=1)).isoformat()
    rows = [
        {"id": "cut", "found_at": fresh, "description": "x" * 1200, "desc_chars": 8000},
        {"id": "whole", "found_at": fresh, "description": "x" * 4000, "desc_chars": 4000},
        {"id": "stub", "found_at": fresh, "description": "x" * 54, "desc_chars": 54},
    ]
    got = [j["id"] for j in scan.backfill_targets(rows, 7, now=now)]
    assert got == ["cut"], got


def test_backfill_leaves_rows_outside_the_window_alone():
    """Scope is the 7 days the dashboard renders, not the 45 the file keeps. The rest are
    postings that expired weeks ago on rows nobody can click."""
    now = scan.datetime.now(scan.timezone.utc)
    rows = [
        {"id": "recent", "found_at": (now - scan.timedelta(days=2)).isoformat(),
         "description": "x" * 1200, "desc_chars": 8000},
        {"id": "stale", "found_at": (now - scan.timedelta(days=30)).isoformat(),
         "description": "x" * 1200, "desc_chars": 8000},
    ]
    assert [j["id"] for j in scan.backfill_targets(rows, 7, now=now)] == ["recent"]
    assert {j["id"] for j in scan.backfill_targets(rows, 45, now=now)} == {"recent", "stale"}


def test_backfill_blanks_the_description_before_fetching():
    """The trap this whole command turns on. fill_description() returns early when what it
    is handed already clears MIN_DESC_CHARS (900), and a stored sample is 1200 -- so passing
    the row through untouched gets the sample handed straight back, no fetch attempted."""
    seen = []
    def fake(job):
        seen.append(job.get("description"))
        return "recovered " * 500
    scan.backfill_row({"url": "https://x/1", "description": "x" * 1200, "desc_chars": 5000}, fake)
    assert seen and all(d == "" for d in seen), seen


def test_backfill_prefers_the_application_form_over_the_advert():
    """Half these rows came through an aggregator, whose url is an advert. apply_url is the
    employer's own board: likelier to answer, likelier to carry the whole ad."""
    tried = []
    def fake(job):
        tried.append(job["url"])
        return "y" * 9000
    scan.backfill_row({"url": "https://aggregator/advert",
                       "apply_url": "https://boards.greenhouse.io/real",
                       "description": "x" * 1200, "desc_chars": 9000}, fake)
    assert tried[0] == "https://boards.greenhouse.io/real", tried


def test_backfill_stops_asking_once_it_has_the_whole_ad():
    """desc_chars is the length the scorer saw. Reaching it means there is nothing more to
    get, so the second target is not requested."""
    tried = []
    def fake(job):
        tried.append(job["url"])
        return "z" * 6000
    scan.backfill_row({"url": "https://advert", "apply_url": "https://board",
                       "description": "x" * 1200, "desc_chars": 6000}, fake)
    assert len(tried) == 1, tried


def test_backfill_never_shortens_a_row():
    """A fetcher that comes back with a careers page's furniture instead of the posting must
    not be allowed to replace a real sample with something worse."""
    def furniture(job):
        return "Join our mission!"
    got = scan.backfill_row({"url": "https://x/1", "description": "x" * 1200,
                             "desc_chars": 8000}, furniture)
    assert len(got) < 1200          # backfill_row reports what it found...
    # ...and cmd_backfill_jd is what refuses it; mirror that rule here so the guard is pinned
    # even if the caller is refactored.
    assert not len(got) > 1200


def test_backfill_survives_a_fetcher_that_raises():
    """A dead link throws rather than returning empty, and one bad row must not take the
    other seventy-eight down with it."""
    def boom(job):
        raise RuntimeError("connection reset")
    assert scan.backfill_row({"url": "https://dead", "description": "x" * 1200,
                              "desc_chars": 8000}, boom) == ""


def test_a_work_mode_field_only_ever_speaks_for_a_us_row():
    """The leak, and its guard rails. market_of() can only look inside the location string,
    and almost no source puts the work mode there -- so a US row is gated on location plus
    work mode. Europe and Canada are untouched: remote wording next to a bare European
    country is how remote-EMEA reqs get REJECTED, so tagging one would drop a real role."""
    assert scan.market_of("", scan.gate_location("Grand Rapids, MI", "Remote")) == "US-Remote"
    assert scan.market_of("", scan.gate_location("New York, NY", "Remote")) == "US-Remote"
    # onsite stays out -- the tag states the fact, it does not assume it
    assert scan.market_of("", scan.gate_location("New York, NY", "Onsite")) is None
    # Canada accepts remote and onsite alike, so the tag could only do harm
    assert scan.gate_location("Toronto, ON", "Remote") == "Toronto, ON"
    # Europe: untagged, so these keep resolving exactly as they did
    for loc in ("Amsterdam", "Ireland", "London", "Brussels"):
        assert scan.gate_location(loc, "Remote") == loc, loc
    assert scan.market_of("", scan.gate_location("Ireland", "Remote")) == "IE"
    # no work mode, no change, whatever the row is
    assert scan.gate_location("New York, NY", "") == "New York, NY"


def test_the_us_hiringcafe_searches_already_asked_for_remote():
    """Those rows are remote BY CONSTRUCTION -- hiring.cafe applied the filter at source --
    and the pipeline was re-deriving it from a location string that never carried it. If
    this ever returns "" for a US search, the US funnel is back to zero."""
    for label in ("us-revops", "us-cs"):
        search = scan.APIFY_HIRINGCAFE_SEARCHES[label]
        assert scan.hc_search_work_mode(search) == "Remote", label
    # and the European searches must never come back claiming remote: they pass
    # ("Hybrid", "Onsite", "Field"), and a mixed search says nothing about a single row
    for label in ("revops", "cs-eu-ca", "cs-nl"):
        assert scan.hc_search_work_mode(scan.APIFY_HIRINGCAFE_SEARCHES[label]) == "", label
    # a search with no locations at all claims nothing either
    assert scan.hc_search_work_mode({}) == ""
    assert scan.hc_search_work_mode({"locations": [{"workplace_types": ["Remote"]},
                                                   {"workplace_types": ["Hybrid"]}]}) == ""


def test_a_rows_own_work_mode_is_preferred_and_absence_costs_nothing():
    """hiring.cafe's key for this is not pinned by a fixture, so the row field REFINES the
    search-level answer rather than being relied on. A missing key returns "" and the
    search's guarantee still stands."""
    assert scan.hc_row_work_mode({"workplace_type": "Remote"}) == "Remote"
    assert scan.hc_row_work_mode({"formatted_workplace_type": "Hybrid"}) == "Hybrid"
    assert scan.hc_row_work_mode({"workplace_types": ["Onsite"]}) == "Onsite"
    # ambiguous or absent says nothing rather than guessing
    assert scan.hc_row_work_mode({"workplace_types": ["Remote", "Hybrid"]}) == ""
    assert scan.hc_row_work_mode({}) == ""
    assert scan.hc_row_work_mode({"workplace_type": ""}) == ""


@contextlib.contextmanager
def _patched(obj, name, value):
    """Swap one attribute for the body of a test and put the original back afterwards, so a
    failing assertion cannot leave scan.get() stubbed for every test that runs after it."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def test_a_stub_asks_the_duplicates_it_absorbed_for_the_posting():
    """The fix this was all for. Dedupe collapses copies BEFORE any description is fetched
    and picks the winner on source rank, so a revopsroles stub outranks the hiring.cafe row
    carrying the employer's own Ashby link. The link survives on `also_seen`; this is the
    row finally asking it for the ad."""
    calls = []
    real = "a real posting. " * 200
    def fake_get(url, **kw):
        calls.append(url)
        class R:
            status_code = 200
            text = ('<script type="application/ld+json">'
                    + json.dumps({"@type": "JobPosting", "description": real})
                    + "</script>") if "ashbyhq" in url else "<html>challenge</html>"
            def raise_for_status(self): pass
        return R()
    with _patched(scan, "get", fake_get):
        got = scan.fill_description({
            "url": "https://revopsroles.com/jobs/abc", "source": "revopsroles",
            "description": "Category: RevOps; Seniority: Senior",
            "also_seen": [{"source": "hiring.cafe",
                           "url": "https://jobs.ashbyhq.com/deepl/f232dbed"}],
        })
    assert not scan.is_thin(got), len(got)
    assert any("ashbyhq" in u for u in calls), calls


def test_duplicate_fallback_only_runs_when_the_row_is_still_thin():
    """A row that already has the ad must not spend a request on its duplicates. The whole
    point of doing this at the end of fill_description() rather than the start."""
    calls = []
    def fake_get(url, **kw):
        calls.append(url)
        raise AssertionError("should not fetch")
    with _patched(scan, "get", fake_get):
        got = scan.fill_description({
            "url": "https://x/1", "source": "linkedin",
            "description": "y" * (scan.MIN_DESC_CHARS + 10),
            "also_seen": [{"source": "adzuna", "url": "https://adzuna/1"}],
        })
    assert len(got) == scan.MIN_DESC_CHARS + 10
    assert calls == []


def test_duplicate_fallback_skips_the_row_own_url_and_is_bounded():
    """A duplicate pointing back at the winner's own link buys nothing, and the budget is a
    budget: also_seen is ordered as dedupe absorbed it, not by how likely each is to answer."""
    tried = []
    def fake_fill(job):
        tried.append(job["url"])
        return ""
    with _patched(scan, "fill_description", fake_fill):
        scan.fill_from_duplicates({
            "url": "https://same",
            "also_seen": [{"source": "a", "url": "https://same"},
                          {"source": "b", "url": "https://1"},
                          {"source": "c", "url": "https://2"},
                          {"source": "d", "url": "https://3"},
                          {"source": "e", "url": "https://4"}],
        })
    assert "https://same" not in tried, tried
    assert len(tried) <= scan.MAX_ALSO_SEEN_FETCHES, tried


def test_backfill_targets_a_stub_that_absorbed_a_duplicate():
    """A stub with a folded-away copy has somewhere new to look; one without does not. That
    qualifier is the difference between healing the dashboard and re-requesting empty pages."""
    now = scan.datetime.now(scan.timezone.utc)
    fresh = (now - scan.timedelta(days=1)).isoformat()
    rows = [
        {"id": "stub-alone", "found_at": fresh, "description": "x" * 54, "desc_chars": 54},
        {"id": "stub-with-dupe", "found_at": fresh, "description": "x" * 54,
         "desc_chars": 54, "also_seen": [{"source": "adzuna", "url": "https://a/1"}]},
    ]
    got = [j["id"] for j in scan.backfill_targets(rows, 7, now=now)]
    assert got == ["stub-with-dupe"], got


def test_backfill_keeps_asking_until_a_stub_has_a_real_posting():
    """A stub's desc_chars is 54, so "as long as the scorer saw" was satisfied by the first
    fetcher that returned anything at all and the row stopped while still holding a stub."""
    tried = []
    def fake(job):
        tried.append(job["url"])
        return "short" if len(tried) == 1 else "w" * 4000
    got = scan.backfill_row({"url": "https://advert", "apply_url": "https://board",
                             "description": "x" * 54, "desc_chars": 54}, fake)
    assert len(tried) == 2, tried
    assert not scan.is_thin(got), len(got)


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
